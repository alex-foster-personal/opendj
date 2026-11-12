# /// script
# requires-python = ">=3.11"
# ///
"""perf-lab heap diff: MemLab's three-snapshot leak method on V8 heap snapshots.

A = after warm-up, B = after N load cycles, C = after N more cycles. An object
is a leak candidate when it was allocated between A and B (its node id is in B
but not in A) and is still alive in C. Objects that only churn (allocated and
freed each cycle) drop out; a planted leak (PerflabPlantedLeak) must show up,
or the method has not been shown able to find anything.

Writes RUN_DIR/heapdiff.json:
  snapshot_total_mb    self size of every node in C (the JS heap the snapshot sees)
  by_constructor_C     top constructors in C by self size (where the JS heap is)
  leak_candidates      constructors of A..B-allocated objects alive in C
  growth_B_to_C        per-constructor self-size growth from B to C
  harness, measurement_deps   the lab code and interpreter that diffed them
                       (manifest.harness_stamp), taken before the first
                       snapshot is read and verified again before the write;
                       if they moved nothing is written and it exits 3.
                       analyze.py uses the diff only under its own harness.

Snapshots are parsed one at a time (each can be 400+ MB of JSON).
Usage: uv run --script scripts/perf/lab/heapdiff.py RUN_DIR
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _load(path: Path) -> tuple[list[int], list[str], dict]:
    with path.open("rb") as fh:
        snap = json.load(fh)
    meta = snap["snapshot"]["meta"]
    return snap["nodes"], snap["strings"], meta


def _iter_nodes(nodes: list[int], strings: list[str], meta: dict):
    fields = meta["node_fields"]
    types = meta["node_types"][0]
    nf = len(fields)
    i_type, i_name, i_id, i_size = (
        fields.index("type"),
        fields.index("name"),
        fields.index("id"),
        fields.index("self_size"),
    )
    for o in range(0, len(nodes), nf):
        t = types[nodes[o + i_type]]
        name = strings[nodes[o + i_name]]
        key = (name if t != "closure" else f"(closure) {name}") if t in ("object", "closure", "native") else f"({t})"
        yield nodes[o + i_id], key, nodes[o + i_size]


def _stamp() -> tuple[str, dict]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from manifest import harness_stamp

    return harness_stamp(())


def main() -> int:
    run = Path(sys.argv[1])
    # a stale diff must not survive a run that refuses
    (run / "heapdiff.json").unlink(missing_ok=True)
    started_as = _stamp()
    a, b, c = (run / f"heap-{n}.heapsnapshot" for n in "ABC")
    for p in (a, b, c):
        if not p.exists():
            print(f"[UNKNOWN] missing {p}", file=sys.stderr)
            return 2
    nodes, strings, meta = _load(a)
    ids_a = {nid for nid, _, _ in _iter_nodes(nodes, strings, meta)}
    del nodes, strings
    nodes, strings, meta = _load(b)
    new_in_b: dict[int, tuple[str, int]] = {}
    size_b: dict[str, int] = defaultdict(int)
    for nid, key, size in _iter_nodes(nodes, strings, meta):
        size_b[key] += size
        if nid not in ids_a:
            new_in_b[nid] = (key, size)
    del nodes, strings, ids_a
    nodes, strings, meta = _load(c)
    count: Counter[str] = Counter()
    self_bytes: Counter[str] = Counter()
    size_c: dict[str, int] = defaultdict(int)
    total = 0
    for nid, key, size in _iter_nodes(nodes, strings, meta):
        size_c[key] += size
        total += size
        hit = new_in_b.get(nid)
        if hit is not None:
            count[hit[0]] += 1
            self_bytes[hit[0]] += hit[1]
    leak = [
        {"name": k, "count": count[k], "self_mb": round(self_bytes[k] / 1048576, 2)}
        for k, _ in self_bytes.most_common(40)
    ]
    out = {
        "snapshot_total_mb": round(total / 1048576, 1),
        "by_constructor_C": [
            {"name": k, "self_mb": round(v / 1048576, 2)} for k, v in sorted(size_c.items(), key=lambda kv: -kv[1])[:30]
        ],
        "leak_candidates": leak,
        "growth_B_to_C": [
            {"name": k, "mb": round((size_c.get(k, 0) - size_b.get(k, 0)) / 1048576, 2)}
            for k in sorted(set(size_c) | set(size_b), key=lambda k: -(size_c.get(k, 0) - size_b.get(k, 0)))[:25]
        ],
        "method": "MemLab three-snapshot: allocated between A and B (node id), alive in C",
        "harness": started_as[0],
        "measurement_deps": started_as[1],
    }
    from manifest import harness_moved

    moved = harness_moved(started_as, _stamp())
    if moved:
        print(f"[FAIL] {moved}; heapdiff.json not written", file=sys.stderr)
        return 3
    (run / "heapdiff.json").write_text(json.dumps(out, indent=1), encoding="utf-8", newline="\n")
    planted = next((x for x in leak if x["name"] == "PerflabPlantedLeak"), None)
    print(f"[OK] heap C {out['snapshot_total_mb']} MB; {len(leak)} candidate constructors; planted leak: {planted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
