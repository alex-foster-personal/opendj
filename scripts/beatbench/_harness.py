"""Shared run loop for every beat-tracking candidate.

NOT A PEP 723 SCRIPT. This is imported by its siblings, which are. Each runner
supplies only the thing that differs -- how to turn one WAV into beat times --
and this supplies everything that must NOT differ: the fixture order, the
timing discipline, the failure handling, and the output schema. Five copies of
a run loop would eventually become five subtly different run loops, and the
differences would show up in the table as if they were analyzer differences.

STDLIB ONLY, so importing it never drags a dependency into a runner's
environment that the runner did not ask for.

TIMING IS SPLIT INTO LOAD AND INFERENCE ON PURPOSE. A neural candidate pays a
one-off model load that a full-corpus run amortises to nothing, so folding it
into per-track cost would misrepresent what the analyzer costs at scale. The
headline figure is the realtime factor -- audio seconds processed per wall
second -- because that is what answers the only question that matters here: can
this be run over the whole library, and how long would that take.

A FAILING TRACK IS RECORDED, NEVER DROPPED SILENTLY. If an analyzer throws on
some file, that is a result about the analyzer. Swallowing it would shrink that
candidate's denominator and quietly improve its scores.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# An analyzer takes a wav path and returns (beats, downbeats, native_bpm).
# downbeats is None when the analyzer has no downbeat concept at all, which is
# reported as N/A rather than as a failure to find any.
Analyzer = Callable[[str], tuple[Sequence[float], Sequence[float] | None, float | None]]


def build_argparser(description: str, default_workers: int) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 means every fixture")
    ap.add_argument("--workers", type=int, default=default_workers)
    return ap


def run(
    args: argparse.Namespace,
    *,
    candidate: str,
    version: str,
    license_note: str,
    shippable: bool,
    emits_downbeats: bool,
    build_analyzer: Callable[[], Analyzer],
) -> int:
    """Run one candidate across the fixture set and write its raw beat times."""
    with open(args.fixtures, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = manifest["fixtures"]
    if args.limit:
        fixtures = fixtures[: args.limit]

    print(f"[{candidate}] loading analyzer", flush=True)
    load_started = time.time()
    analyzer = build_analyzer()
    load_s = round(time.time() - load_started, 3)
    print(
        f"[{candidate}] loaded in {load_s}s, {len(fixtures)} fixtures, "
        f"{args.workers} workers",
        flush=True,
    )

    def one(fixture: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        started = time.time()
        try:
            beats, downbeats, native_bpm = analyzer(fixture["wav"])
        except Exception as exc:
            return fixture["stable_id"], {
                "beats": [],
                "downbeats": None,
                "native_bpm": None,
                "runtime_s": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "traceback": traceback.format_exc()[-600:],
            }
        return fixture["stable_id"], {
            "beats": [round(float(b), 5) for b in beats],
            "downbeats": None if downbeats is None else [round(float(b), 5) for b in downbeats],
            "native_bpm": None if native_bpm is None else round(float(native_bpm), 4),
            "runtime_s": round(time.time() - started, 3),
            "error": None,
        }

    wall_started = time.time()
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (stable_id, row) in enumerate(pool.map(one, fixtures), 1):
            results[stable_id] = row
            if i % 25 == 0:
                done = time.time() - wall_started
                print(f"[{candidate}]   {i}/{len(fixtures)} in {done:.0f}s", flush=True)
    wall_s = time.time() - wall_started

    audio_s = sum(f["window_end_s"] - f["window_start_s"] for f in fixtures)
    runtimes = sorted(r["runtime_s"] for r in results.values() if not r["error"])
    failures = [sid for sid, r in results.items() if r["error"]]

    payload = {
        "schema": 1,
        "candidate": candidate,
        "candidate_version": version,
        "license": license_note,
        "shippable": shippable,
        "emits_downbeats": emits_downbeats,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workers": args.workers,
        "model_load_s": load_s,
        "wall_s": round(wall_s, 2),
        "audio_s": round(audio_s, 1),
        "realtime_factor": round(audio_s / wall_s, 2) if wall_s else None,
        "per_track_runtime_s": {
            "p50": runtimes[len(runtimes) // 2] if runtimes else None,
            "p95": runtimes[int(0.95 * (len(runtimes) - 1))] if runtimes else None,
        },
        "n_fixtures": len(fixtures),
        "n_failed": len(failures),
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(
        f"[{candidate}] done: {wall_s:.0f}s wall, {payload['realtime_factor']}x realtime, "
        f"{len(failures)} failed -> {args.out}",
        flush=True,
    )
    if failures:
        first = results[failures[0]]["error"]
        print(f"[{candidate}]   first failure: {first}", flush=True)
    return 0
