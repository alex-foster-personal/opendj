"""Round-1 candidate: the beatgrid lane's OWN producer, measured as it will ship.

NOT A PARALLEL IMPLEMENTATION OF BEAT_THIS. `scripts/beatbench/run_beat_this.py`
is the round-0 artifact and is left exactly as it was, pinned to the git-main
checkout it measured. This runner instead shells out to
`apps/analysis_beatgrid/beat_this_runner.py`, the file that IS the backfill
producer, so what the table scores is the code the deck will eventually run
rather than a benchmark-shaped lookalike. If the producer regresses, the bench
is supposed to notice, and it cannot if the two are separate implementations.

STDLIB ONLY, NO PEP 723 BLOCK. The heavy environment belongs to the producer;
this file only invokes it and reshapes its JSON into the schema
`scripts/beatbench/report.py` reads. That keeps torch out of one more place.

THRESHOLD IS THE ROUND-1 VARIABLE. beat_this's own postprocessor hardcodes its
keep-threshold at probability 0.5. Round 0 measured the consequence: 6 of 137
dynamic tracks gridded not at all and 42 under-gridded. Passing `--threshold`
produces a second candidate row from the SAME model and the SAME fixtures, so
the delta between the two rows is attributable to the threshold alone.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

from apps.analysis_beatgrid.bar_phase import assign_bar_phase
from scripts.beatbench._harness import resolve_fixture_paths

RUNNER = os.path.join("apps", "analysis_beatgrid", "beat_this_runner.py")

# The producer emits beats and downbeats; the schema it emits them under is
# asserted rather than assumed, because a silently older raw file would be
# missing the weights digest and the bar-phase change alike.
PRODUCER_SCHEMA = 2


def _trimmed_manifest(manifest: dict, fixtures: list, raw_out: str) -> str:
    """Write a limited run's own manifest beside its output, never the round's.

    A partial manifest sitting in the round directory would be one careless
    `--fixtures` away from being read as the round's fixture set.

    `paths_relative_to` is DROPPED rather than copied. `fixtures` arrives here
    already resolved, and this file lands beside the raw output rather than
    beside the bundle it may have come from, so carrying the declaration
    forward would re-resolve absolute paths against the wrong directory the
    moment they stopped being absolute.
    """
    path = raw_out + ".manifest.json"
    trimmed = {k: v for k, v in manifest.items() if k != "paths_relative_to"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({**trimmed, "fixtures": fixtures}, fh)
    return path


def _producer_command(args: argparse.Namespace, manifest_path: str) -> list[str]:
    cmd = [
        "uv", "run", "--no-project", "--script", RUNNER,
        "--manifest", manifest_path,
        "--out", args.raw_out,
        "--device", args.device,
        "--threshold", str(args.threshold),
        "--measure-rss",
    ]
    if args.activations_dir:
        cmd += ["--activations-dir", args.activations_dir]
    if args.torch_threads:
        cmd += ["--torch-threads", str(args.torch_threads)]
    return cmd


def _reshape(raw: dict, fixtures: list, label: str) -> dict[str, Any]:
    """Producer JSON (keyed by wav path) into harness JSON (keyed by stable_id).

    Refuses to return a partial set: a candidate missing rows would shrink its
    own denominator and quietly improve its scores.
    """
    by_wav = {f["wav"]: f for f in fixtures}
    results: dict[str, Any] = {}
    for wav_path, row in raw["results"].items():
        fixture = by_wav.get(wav_path)
        if fixture is None:
            continue
        # Bar numbering is POLICY, applied here from the pure module rather
        # than inside the torch environment, so what the table scores is the
        # same 1..4 assignment the deck will read. See bar_phase.py.
        phase = assign_bar_phase(row.get("beats") or [], row.get("downbeats") or [])
        results[fixture["stable_id"]] = {
            # Both sides are excerpt-relative, because the producer reads the
            # excerpt WAV, so no offset is applied here. window_start_s rides
            # along so a reader can see it was considered, not forgotten.
            "beats": row.get("beats", []),
            "downbeats": row.get("downbeats"),
            "beat_numbers": phase.beat_numbers,
            "bar_phase_unestablished": phase.bar_phase_unestablished,
            "bar_phase_reason": phase.reason,
            "n_backprojected_beats": phase.n_backprojected_beats,
            "n_bars_over_length": phase.n_bars_over_length,
            "n_bars_under_length": phase.n_bars_under_length,
            "max_bar_beats": phase.max_bar_beats,
            "activation_peak": row.get("activation_peak"),
            "native_bpm": None,
            "runtime_s": row.get("runtime_s"),
            "error": row.get("error"),
            "window_start_s": fixture["window_start_s"],
        }

    missing = [f["stable_id"] for f in fixtures if f["stable_id"] not in results]
    if missing:
        raise SystemExit(
            f"[{label}] {len(missing)} fixtures produced no row at all "
            f"(first: {missing[:3]}); refusing to write a partial candidate"
        )
    return results


def _payload(
    args: argparse.Namespace,
    raw: dict,
    fixtures: list,
    results: dict[str, Any],
    wall_s: float,
) -> dict[str, Any]:
    audio_s = sum(f["window_end_s"] - f["window_start_s"] for f in fixtures)
    runtimes = sorted(r["runtime_s"] for r in results.values() if not r["error"])
    return {
        "schema": 1,
        "candidate": args.label,
        "candidate_version": (
            f"beat-this {raw['beat_this_version']} (torch {raw['torch_version']}, "
            f"{raw['device']}, minimal postproc, threshold {raw['threshold']}, "
            f"weights {raw['model_sha256'][:16]})"
        ),
        "license": "MIT (code AND weights)",
        "shippable": True,
        "emits_downbeats": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workers": 1,
        "model_load_s": raw["model_load_s"],
        "wall_s": round(wall_s, 2),
        "audio_s": round(audio_s, 1),
        "realtime_factor": round(audio_s / wall_s, 2) if wall_s else None,
        "peak_rss_mb": raw.get("peak_rss_mb"),
        "producer": raw["producer"],
        "producer_version": raw["producer_version"],
        # THE CHECKPOINT NAME IS NOT PROVENANCE. `final0` resolves to a file in
        # the torch hub cache that can be replaced, so the digest of the bytes
        # actually loaded rides into every scored artifact, not just the raw
        # one (Codex P1 on PR #1514). Read straight from the producer's payload
        # with no default: a missing key must fail here rather than write a
        # candidate whose weights nobody can identify.
        "model": {
            "checkpoint": raw["checkpoint"],
            "checkpoint_path": raw["checkpoint_path"],
            "model_sha256": raw["model_sha256"],
            "beat_this_version": raw["beat_this_version"],
            "torch_version": raw["torch_version"],
            "device": raw["device"],
        },
        "threshold": raw["threshold"],
        "per_track_runtime_s": {
            "p50": runtimes[len(runtimes) // 2] if runtimes else None,
            "p95": runtimes[int(0.95 * (len(runtimes) - 1))] if runtimes else None,
        },
        "n_fixtures": len(fixtures),
        "n_failed": sum(1 for r in results.values() if r["error"]),
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--out", required=True, help="candidate JSON in the harness schema")
    ap.add_argument("--raw-out", required=True, help="the producer's own JSON, kept verbatim")
    ap.add_argument("--label", required=True, help="candidate name in the report table")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--activations-dir")
    ap.add_argument("--torch-threads", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    with open(args.fixtures, encoding="utf-8") as fh:
        manifest = json.load(fh)
    # Resolved HERE so `by_wav` in `_reshape` is keyed by the same strings the
    # producer was handed; a bundle's manifest-relative paths would otherwise
    # match nothing and every row would be dropped.
    fixtures = resolve_fixture_paths(manifest, args.fixtures)
    if args.limit:
        fixtures = fixtures[: args.limit]

    # The producer takes a manifest, so a limited run needs a trimmed one.
    manifest_path = (
        _trimmed_manifest(manifest, fixtures, args.raw_out) if args.limit else args.fixtures
    )
    cmd = _producer_command(args, manifest_path)
    print(f"[{args.label}] {' '.join(cmd)}", flush=True)

    started = time.time()
    # check=True: an analyzer that failed to RUN is not a candidate that scored
    # badly, and the two must never be confused in a table.
    subprocess.run(cmd, check=True)
    wall_s = time.time() - started

    with open(args.raw_out, encoding="utf-8") as fh:
        raw = json.load(fh)
    if raw.get("schema") != PRODUCER_SCHEMA:
        raise SystemExit(
            f"[{args.label}] producer wrote schema {raw.get('schema')!r}, expected "
            f"{PRODUCER_SCHEMA}; refusing to score an artifact this file cannot read"
        )
    if not raw.get("model_sha256"):
        raise SystemExit(
            f"[{args.label}] the producer could not resolve the checkpoint digest "
            f"(checkpoint={raw.get('checkpoint')!r}); refusing to write a candidate "
            "whose weights cannot be identified"
        )

    results = _reshape(raw, fixtures, args.label)
    payload = _payload(args, raw, fixtures, results, wall_s)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(
        f"[{args.label}] {len(results)} tracks, {payload['n_failed']} failed, "
        f"{wall_s:.0f}s wall, {payload['realtime_factor']}x realtime -> {args.out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
