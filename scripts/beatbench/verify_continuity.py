#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mir_eval>=0.8", "numpy<2"]
# ///
"""Control: cross-check this repo's CMLt/AMLt against mir_eval on REAL beat times.

WHY THIS EXISTS. `scripts/beatbench/scorer.py` reimplements the continuity
metrics in pure stdlib, because the scorer is imported by pytest in the repo
venv while every analyzer lives in a throwaway PEP 723 environment, and a
scorer that dragged in a scientific stack would be a scorer that could differ
between rounds. A reimplementation of a published metric is exactly the kind of
thing that passes its own synthetic tests and is still subtly wrong, so it
needs a control that can FAIL against the reference implementation.

WHY IT RUNS ON REAL BEAT TIMES AND NOT ON SYNTHETIC ONES. The synthetic cases
are already pinned in `tests/beatbench/test_scorer_v11.py`, and they were
written by the same person as the implementation, so they can share its blind
spots. Real candidate output is the population the metric will actually be
quoted over: sparse grids, dropped beats, half-tempo stretches, ties. If the
two implementations agree there, the disagreement space that matters is empty.

NOT A TEST IN THE SUITE. mir_eval is a benchmark-only dependency and never
enters the repo venv, and this needs a round's committed candidate JSON to run
against. It is run by hand per round and its output is recorded in the
experiment log with the round it validated.

Exit code is nonzero when any track disagrees by more than the float tolerance,
so it can gate a round rather than merely inform one.
"""

from __future__ import annotations

import argparse
import json
import sys

import mir_eval  # type: ignore[import-not-found]  # PEP 723 dep, not in the repo venv
import numpy as np

# Both implementations do the same float arithmetic in a different order, so
# only rounding should separate them. Anything larger is a real disagreement.
AGREEMENT_TOLERANCE = 1e-9


def _window(times, start: float, end: float) -> np.ndarray:
    return np.array([t for t in times if start <= t < end], dtype=float)


def _compare_one(stable_id, fixture, result, score_continuity):
    """One track: `(cmlt_delta, amlt_delta, disagreement_note)`, or None to skip.

    Returns None for a track that cannot be compared at all -- no fixture, a
    runner error, or too few beats on either side -- so an unmeasurable track
    shrinks the denominator instead of counting as agreement.
    """
    if fixture is None or result.get("error"):
        return None

    start, end = fixture["score_start_s"], fixture["score_end_s"]
    offset = fixture["window_start_s"]
    reference = _window([b[1] for b in fixture["ref_beats"]], start, end)
    estimated = _window([t + offset for t in result["beats"]], start, end)
    if reference.size < 4 or estimated.size < 4:
        return None

    ours = score_continuity(reference.tolist(), estimated.tolist())
    _cmlc, theirs_cmlt, _amlc, theirs_amlt = mir_eval.beat.continuity(reference, estimated)

    d_cmlt = abs(ours.cmlt - theirs_cmlt)
    d_amlt = abs(ours.amlt - theirs_amlt)
    note = ""
    if d_cmlt > AGREEMENT_TOLERANCE or d_amlt > AGREEMENT_TOLERANCE:
        note = (
            f"{stable_id}: CMLt ours {ours.cmlt:.6f} mir_eval {theirs_cmlt:.6f} | "
            f"AMLt ours {ours.amlt:.6f} mir_eval {theirs_amlt:.6f}"
        )
    return d_cmlt, d_amlt, note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--candidate", required=True, help="a candidate-*.json from this round")
    ap.add_argument("--limit", type=int, default=0, help="0 means every fixture")
    args = ap.parse_args()

    # Imported here rather than at the top: this script runs in its own PEP 723
    # environment, and the repo root is only importable because uv runs it from
    # there. Keeping it beside its use makes the coupling visible.
    sys.path.insert(0, ".")
    from scripts.beatbench.scorer import score_continuity

    with open(args.fixtures, encoding="utf-8") as fh:
        fixtures = {f["stable_id"]: f for f in json.load(fh)["fixtures"]}
    with open(args.candidate, encoding="utf-8") as fh:
        candidate = json.load(fh)

    checked = skipped = 0
    worst_cmlt = worst_amlt = 0.0
    disagreements: list[str] = []

    for stable_id, result in candidate["results"].items():
        if args.limit and checked >= args.limit:
            break
        compared = _compare_one(
            stable_id, fixtures.get(stable_id), result, score_continuity
        )
        if compared is None:
            skipped += 1
            continue
        d_cmlt, d_amlt, note = compared
        worst_cmlt = max(worst_cmlt, d_cmlt)
        worst_amlt = max(worst_amlt, d_amlt)
        if note:
            disagreements.append(note)
        checked += 1

    print(f"[verify-continuity] mir_eval {mir_eval.__version__}", flush=True)
    print(f"[verify-continuity] compared {checked} tracks, skipped {skipped}", flush=True)
    print(
        f"[verify-continuity] worst absolute difference: "
        f"CMLt {worst_cmlt:.3e}, AMLt {worst_amlt:.3e}",
        flush=True,
    )

    if checked == 0:
        # Zero is both a value and an error signature. A run that compared
        # nothing has not agreed with anything, and must never read as a pass.
        print("[verify-continuity] FAILED: compared 0 tracks, so nothing was verified")
        return 2
    if disagreements:
        print(f"[verify-continuity] FAILED: {len(disagreements)} of {checked} disagree")
        for line in disagreements[:20]:
            print(f"[verify-continuity]   {line}")
        return 1
    print(f"[verify-continuity] OK: all {checked} tracks agree within {AGREEMENT_TOLERANCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
