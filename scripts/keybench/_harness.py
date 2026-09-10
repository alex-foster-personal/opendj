"""Shared run loop for every key-lane candidate.

NOT A PEP 723 SCRIPT. This is imported by its siblings. Each runner supplies
only the thing that differs -- how to turn one WAV into a key answer -- and
this supplies everything that must NOT differ: the fixture order, the timing
discipline, the failure handling, and the output schema.

STDLIB ONLY, so importing it never drags a dependency into a runner's
environment that the runner did not ask for.

A FAILING TRACK IS RECORDED, NEVER DROPPED SILENTLY. If an analyzer throws on
some file, that is a result about the analyzer. Swallowing it would shrink that
candidate's denominator and quietly improve its scores.

HEADLINE PER-TRACK TIME IS WARM INFERENCE, not model load. A neural candidate
pays a one-off load that a full-corpus run amortises to nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from collections.abc import Callable
from typing import Any

PATHS_RELATIVE_TO_MANIFEST = "manifest"

# An analyzer takes a wav path and returns the per-fixture result dict
# (key_camelot / key_openkey / flags / error). The harness adds runtime_s.
Analyzer = Callable[[str], dict[str, Any]]


def resolve_fixture_paths(manifest: dict[str, Any], manifest_path: str) -> list[dict[str, Any]]:
    """Fixtures whose `wav` a candidate can actually open, whatever the cwd is.

    A portable bundle stores `wav/<stable_id>.wav` relative to ITSELF and
    declares `paths_relative_to: manifest`. An unrecognised value is refused
    rather than defaulted. Absolute paths pass through `os.path.join`
    untouched. A missing `wav` key is left as-is so a throwing analyzer can
    still record a result instead of aborting the whole arm.
    """
    fixtures = list(manifest["fixtures"])
    declared = manifest.get("paths_relative_to")
    if declared is None:
        return fixtures
    if declared != PATHS_RELATIVE_TO_MANIFEST:
        raise SystemExit(
            f"[keybench] {manifest_path} declares paths_relative_to "
            f"{declared!r}, which this harness does not know how to resolve"
        )
    base = os.path.dirname(os.path.abspath(manifest_path))
    resolved: list[dict[str, Any]] = []
    for fixture in fixtures:
        row = dict(fixture)
        wav = row.get("wav")
        if wav:
            row["wav"] = os.path.join(base, wav)
        resolved.append(row)
    return resolved


def build_argparser(description: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--fixtures", required=True, help="sealed bundle manifest.json")
    ap.add_argument("--out", required=True, help="arm JSON output path")
    ap.add_argument("--limit", type=int, default=0, help="0 means every fixture")
    ap.add_argument("--device", default="cpu")
    return ap


def run(
    args: argparse.Namespace,
    *,
    candidate: str,
    version: str,
    device: str,
    build_analyzer: Callable[[], Analyzer],
    extra_envelope: dict[str, Any] | None = None,
) -> int:
    """Run one candidate across the fixture set and write its arm JSON."""
    with open(args.fixtures, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = resolve_fixture_paths(manifest, args.fixtures)
    if args.limit:
        fixtures = fixtures[: args.limit]

    print(f"[{candidate}] loading analyzer", flush=True)
    load_started = time.time()
    analyzer = build_analyzer()
    load_s = round(time.time() - load_started, 3)
    print(
        f"[{candidate}] loaded in {load_s}s, {len(fixtures)} fixtures, device={device}",
        flush=True,
    )

    results: dict[str, Any] = {}
    wall_started = time.time()
    for i, fixture in enumerate(fixtures, 1):
        stable_id = fixture["stable_id"]
        started = time.time()
        try:
            row = dict(analyzer(fixture["wav"]))
        except Exception as exc:
            row = {
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "traceback": traceback.format_exc()[-600:],
            }
        else:
            row.setdefault("error", None)
        row["runtime_s"] = round(time.time() - started, 3)
        results[stable_id] = row
        if i % 25 == 0:
            done = time.time() - wall_started
            print(f"[{candidate}]   {i}/{len(fixtures)} in {done:.0f}s", flush=True)
    wall_s = time.time() - wall_started

    runtimes = sorted(r["runtime_s"] for r in results.values() if not r.get("error"))
    failures = [sid for sid, r in results.items() if r.get("error")]

    payload: dict[str, Any] = {
        "schema": 1,
        "candidate": candidate,
        "candidate_version": version,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_fixtures": len(fixtures),
        "n_failed": len(failures),
        "device": device,
        "model_load_s": load_s,
        "wall_s": round(wall_s, 2),
        "per_track_runtime_s": {
            "p50": runtimes[len(runtimes) // 2] if runtimes else None,
            "p95": runtimes[int(0.95 * (len(runtimes) - 1))] if runtimes else None,
        },
        "results": results,
    }
    if extra_envelope:
        payload.update(extra_envelope)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(
        f"[{candidate}] done: {wall_s:.0f}s wall, {len(failures)} failed -> {args.out}",
        flush=True,
    )
    if failures:
        first = results[failures[0]]["error"]
        print(f"[{candidate}]   first failure: {first}", flush=True)
    return 0
