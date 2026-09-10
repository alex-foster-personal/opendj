#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mir_eval>=0.8"]
# ///
"""Control: cross-check this repo's weighted_score against real mir_eval.

WHY THIS EXISTS. `apps/analysis_bench/scorers/key_lane.py` reimplements the
MIREX weighted key score in pure stdlib, because the scorer is imported by
pytest in the repo venv and `mir_eval` is a benchmark-only dependency that
must never enter it. A reimplementation of a published metric is exactly the
kind of thing that passes its own synthetic tests and is still subtly wrong,
so it needs a control that can FAIL against the reference implementation.

WHY THE DOMAIN IS ENUMERATED RATHER THAN SAMPLED. Unlike beatgrid's continuity
metrics (`verify_continuity.py`, which needs REAL beat times because the
interesting disagreements live in continuous timing edge cases), a key pair
is one of exactly 24 x 24 = 576 combinations. Enumerating all of them is not
weaker than sampling real data here, it is exhaustive: every category the
metric can ever return is covered.

THE ONE DOCUMENTED, EXPECTED DIVERGENCE. `key_lane.py`'s module docstring
records that this scorer deliberately credits BOTH directions of the fifth
(issue #1584 and specs/native-analysis-v1-lanes/nav1-key-r0.md both ask for
"MIREX weighted, descending fifths allowed"), while the real, installed
mir_eval 0.8.2 credits the ascending fifth only. This script asserts that
divergence is the ONLY one: every other one of the 576 pairs must agree with
mir_eval exactly, and the descending-fifth pairs must disagree by exactly
0.5 (ours) vs 0.0 (mir_eval), never by anything else.

NOT A TEST IN THE SUITE. mir_eval never enters the repo venv. Run by hand per
round; its output (agreement count, the one documented divergence, mir_eval's
version) is recorded in the experiment log alongside the round it validated.
"""

from __future__ import annotations

import sys

import mir_eval  # type: ignore[import-not-found]  # PEP 723 dep, not in the repo venv

_PITCH_NAMES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]


def _mir_eval_string(pitch_class: int, is_minor: bool) -> str:
    return f"{_PITCH_NAMES[pitch_class]} {'minor' if is_minor else 'major'}"


def main() -> int:
    # Imported here, not at the top: this script runs in its own PEP 723
    # environment, and the repo root is only importable because uv runs it
    # from there. Keeping it beside its use makes the coupling visible.
    sys.path.insert(0, ".")
    from apps.analysis_bench.scorers.key_lane import weighted_score
    from apps.analysis_key.canon import Key

    agreements = 0
    descending_fifths: list[tuple[str, str]] = []
    unexpected: list[str] = []

    for ref_pc in range(12):
        for ref_minor in (False, True):
            reference = Key(ref_pc, ref_minor)
            for est_pc in range(12):
                for est_minor in (False, True):
                    estimated = Key(est_pc, est_minor)
                    ours = weighted_score(reference, estimated)
                    theirs = mir_eval.key.weighted_score(
                        _mir_eval_string(ref_pc, ref_minor),
                        _mir_eval_string(est_pc, est_minor),
                    )
                    if ours == theirs:
                        agreements += 1
                        continue
                    delta = (est_pc - ref_pc) % 12
                    is_descending_fifth = est_minor == ref_minor and delta == 5
                    if is_descending_fifth and ours == 0.5 and theirs == 0.0:
                        descending_fifths.append((
                            _mir_eval_string(ref_pc, ref_minor),
                            _mir_eval_string(est_pc, est_minor),
                        ))
                        continue
                    unexpected.append(
                        f"{_mir_eval_string(ref_pc, ref_minor)} -> "
                        f"{_mir_eval_string(est_pc, est_minor)}: ours {ours}, mir_eval {theirs}"
                    )

    total = 24 * 24
    print(f"[verify-mirex] mir_eval {mir_eval.__version__}")
    print(f"[verify-mirex] compared {total} of {total} (reference, estimated) key pairs")
    print(f"[verify-mirex] exact agreement: {agreements}")
    print(
        f"[verify-mirex] documented divergence (descending fifths, ours 0.5 vs "
        f"mir_eval 0.0): {len(descending_fifths)}"
    )

    if unexpected:
        print(f"[verify-mirex] FAILED: {len(unexpected)} pair(s) disagree unexpectedly")
        for line in unexpected[:20]:
            print(f"[verify-mirex]   {line}")
        return 1
    if len(descending_fifths) != 24:
        # Same mode, delta == 5, both pitch classes free: exactly 12 pcs x 2
        # modes = 24 such pairs exist in the 576-pair grid.
        print(
            f"[verify-mirex] FAILED: expected exactly 24 descending-fifth pairs, "
            f"found {len(descending_fifths)}"
        )
        return 1
    print(f"[verify-mirex] OK: {agreements} exact agreements, "
          f"24 documented descending-fifth divergences, 0 unexpected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
