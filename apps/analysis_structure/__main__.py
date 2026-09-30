"""``python -m apps.analysis_structure``: analyze song structure, or show one track's sections.

    python -m apps.analysis_structure run --stable-id ID [--stable-id ID ...] [--device auto]
    python -m apps.analysis_structure run --missing --limit 50
    python -m apps.analysis_structure show ID

``run`` needs `uv` on PATH (the model runs in its own PEP 723 environment) and
a canonical own beatgrid for each track; a track without one is written as
``status: failed, reason: no_own_beatgrid`` rather than skipped.

-Claude
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import store as analysis_store
from apps.shared.state import db as state_db
from apps.shared.state.locations import bulk_local_audio_paths

from .pipeline import analyze, cache_dir, load_sidecar


def _missing_ids(conn, data_dir: Path, limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT stable_id FROM analysis_canonical WHERE lane = 'beatgrid' ORDER BY stable_id"
    ).fetchall()
    done = {p.stem for p in cache_dir(data_dir).glob("*.json")}
    return [r[0] for r in rows if r[0] not in done][:limit]


def _cmd_run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else rb_config.DATA_DIR
    conn = analysis_store.open_conn(Path(args.state_db) if args.state_db else None)
    try:
        ids = list(args.stable_id) or (
            _missing_ids(conn, data_dir, args.limit) if args.missing else []
        )
        if not ids:
            print("nothing to analyze: pass --stable-id or --missing", file=sys.stderr)
            return 2
        state = state_db.open_ro(Path(args.state_db) if args.state_db else None)
        try:
            paths = bulk_local_audio_paths(state, ids)
        finally:
            state.close()
        docs = analyze(conn, ids, paths, data_dir, device=args.device)
    finally:
        conn.close()
    ok = sum(d["status"] == "ok" for d in docs)
    for d in docs:
        print(
            json.dumps(
                {
                    "stable_id": d["stable_id"],
                    "status": d["status"],
                    "reason": d.get("reason"),
                    "sections": len(d.get("sections", [])),
                    "implausible": d.get("quantize", {}).get("implausible"),
                }
            )
        )
    print(f"{ok} of {len(docs)} analyzed ok; sidecars in {cache_dir(data_dir)}", file=sys.stderr)
    return 0 if ok else 1


def _cmd_show(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else rb_config.DATA_DIR
    doc = load_sidecar(data_dir, args.stable_id)
    if doc is None:
        print(f"no structure sidecar for {args.stable_id}", file=sys.stderr)
        return 1
    if doc["status"] != "ok":
        print(f"failed: {doc.get('reason')} {doc.get('detail') or ''}".rstrip())
        return 1
    for s in doc["sections"]:
        bars = "" if s["bars"] is None else f"{s['bars']:>4d} bars"
        bar = "" if s["start_bar"] is None else f"bar {s['start_bar'] + 1:>4d}"
        print(f"{s['start_s']:8.2f}s  {bar:9s} {bars:10s} {s['label']}")
    q = doc["quantize"]
    print(
        f"phrase offset {q['phrase_offset']}, merged {q['merged']}, "
        f"flags {q['implausible'] or 'none'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m apps.analysis_structure")
    ap.add_argument("--data-dir", help="defaults to MDT_DATA_DIR or the repo data/")
    ap.add_argument("--state-db", help="defaults to <data>/state/state.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="analyze tracks and write structure sidecars")
    run.add_argument("--stable-id", action="append", default=[])
    run.add_argument(
        "--missing", action="store_true", help="tracks with an own beatgrid and no sidecar"
    )
    run.add_argument("--limit", type=int, default=50)
    run.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda", "mps"))
    run.set_defaults(func=_cmd_run)
    show = sub.add_parser("show", help="print one track's sections")
    show.add_argument("stable_id")
    show.set_defaults(func=_cmd_show)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
