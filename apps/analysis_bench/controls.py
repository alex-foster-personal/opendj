"""The control arms: the floor and the ceiling every table is read against.

WHY CONTROLS ARE A COMMAND AND NOT A FLAG. A control has to be produced the
same way a candidate is -- same bundle, same envelope, same scorer -- or it is
not measuring the same thing. Running it as its own arm means the control's row
comes through every step the candidate's row does, so a break in the harness
shows up in the control instead of silently flattering the candidate.

THE NEGATIVE CONTROL says what a candidate gets for free. Beatgrid's lives in
`scripts/beatbench/run_constant_bpm.py`, unchanged since round 1 published its
0.280 F floor, and is referenced rather than reimplemented here so the floor
cannot drift.

THE POSITIVE CONTROL says the ruler can still measure a good answer. For
beatgrid it is the reference grid displaced by a constant +25 ms: it must score
near-perfect on shift-corrected F and visibly worse on raw F, which is exactly
the pair scorer v1.1.0 added. It CAN fail -- if the truth join, the windowing or
the shift correction breaks, this arm stops being near-perfect -- which is the
whole point of a control.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Small enough to sit inside the +/-70 ms beat tolerance is NOT what is wanted
# here: 25 ms is deliberately a third of the tolerance, so raw F stays high
# while the median-shift correction still has something to remove.
BEATGRID_OFFSET_S = 0.025


def _beatgrid_positive(fixtures_path: Path) -> dict[str, Any]:
    from scripts.beatbench import report as beatgrid_report

    _, fixtures = beatgrid_report.load_fixtures(str(fixtures_path))
    results = {
        stable_id: {
            # Candidates emit WINDOW-relative times (the scorer adds window_start_s
            # back); the truth is stored in absolute track time.
            "beats": [
                round(beat[1] - fixture["window_start_s"] + BEATGRID_OFFSET_S, 5)
                for beat in fixture["ref_beats"]
            ],
            "downbeats": None,
            "native_bpm": None,
            "runtime_s": 0.0,
            "error": None,
        }
        for stable_id, fixture in fixtures.items()
    }
    return {
        "candidate": "truth_offset_25ms",
        "candidate_version": (
            f"control, reference grid displaced by +{BEATGRID_OFFSET_S * 1000:.0f} ms"
        ),
        "emits_downbeats": False,
        "results": results,
    }


# C major, not an arbitrary constant: spec section 5's "Agreement metric"
# bullet names this exact control by citation -- "Include a constant
# predictor control (S-KEY's 'always C major' floor was 19.0 MIREX)" -- so
# the value is the spec's, not this module's choice. Camelot 8B, checked
# against apps.analysis_key.canon in tests/analysis_bench/test_key_lane.py so
# the literal here cannot drift from what the scorer parses it as. (A prior
# cut of this control answered A minor / 8A instead, which matches no cited
# source -- Codex P2 BLOCKING, PR #1620.)
KEY_CONSTANT_CAMELOT = "8B"


def _key_negative(fixtures_path: Path) -> dict[str, Any]:
    from apps.analysis_bench.scorers import key_lane

    _, rekordbox, _mik = key_lane.load_bundle(fixtures_path.parent)
    results = {stable_id: {"key_camelot": KEY_CONSTANT_CAMELOT} for stable_id in rekordbox}
    return {
        "candidate": "constant_key",
        "candidate_version": f"control, always answers Camelot {KEY_CONSTANT_CAMELOT} (C major)",
        "results": results,
    }


def _key_most_common_negative(fixtures_path: Path) -> dict[str, Any]:
    """The bundle's second named floor (`specs/native-analysis-v1-lanes/
    nav1-key-r0.md`: "controls: constant 'always C major', constant 'most
    common key in the set'"). `constant_key` alone only covers the first of
    those two; a full round needs both (Codex P2 BLOCKING, PR #1620).

    Counted over the REKORDBOX reference (the lane's own `truth` field lists
    rekordbox first), ties broken by first-seen order in the bundle's fixture
    list -- `collections.Counter.most_common` is stable, so this is
    deterministic and reproducible rather than depending on dict-iteration
    happenstance a reader would have to trust.
    """
    from collections import Counter

    from apps.analysis_bench.scorers import key_lane

    _, rekordbox, _mik = key_lane.load_bundle(fixtures_path.parent)
    present = [key for key in rekordbox.values() if key is not None]
    if not present:
        raise ValueError("the most_common control needs at least one rekordbox reference")
    winner = Counter(present).most_common(1)[0][0]
    camelot = key_lane.canon.to_camelot(winner)
    results = {stable_id: {"key_camelot": camelot} for stable_id in rekordbox}
    return {
        "candidate": "most_common_key",
        "candidate_version": (
            f"control, always answers {camelot}, the most common rekordbox key in this bundle"
        ),
        "results": results,
    }


def _key_positive(fixtures_path: Path) -> dict[str, Any]:
    from apps.analysis_bench.scorers import key_lane

    _, rekordbox, _mik = key_lane.load_bundle(fixtures_path.parent)
    # A fixture with no rekordbox reference is skipped, not fabricated: this
    # control's job is to echo the reference, and there is nothing to echo.
    # It still counts as an honest omission when scored (key_lane.py's
    # n_omitted), not a silently smaller denominator.
    results = {
        stable_id: {"key_camelot": key_lane.canon.to_camelot(key)}
        for stable_id, key in rekordbox.items()
        if key is not None
    }
    return {
        "candidate": "truth_echo",
        "candidate_version": "control, echoes the rekordbox reference key",
        "results": results,
    }


def _key_positive_mik(fixtures_path: Path) -> dict[str, Any]:
    """The MIK-side ceiling. `truth_echo` echoes rekordbox, so on any fixture
    where the two references disagree it is NOT a ceiling for `vs_mik` --
    that column would then carry no control that can score perfectly, which
    is a real gap (Codex P2 BLOCKING, PR #1620), not a documented property of
    "neither reference is truth": a table needs a floor and a ceiling for
    EVERY column it reports, not just the rekordbox ones.
    """
    from apps.analysis_bench.scorers import key_lane

    _, _rekordbox, mik = key_lane.load_bundle(fixtures_path.parent)
    results = {
        stable_id: {"key_camelot": key_lane.canon.to_camelot(key)}
        for stable_id, key in mik.items()
        if key is not None
    }
    return {
        "candidate": "truth_echo_mik",
        "candidate_version": "control, echoes the MIK reference key",
        "results": results,
    }


_BUILDERS = {
    ("beatgrid", "positive"): _beatgrid_positive,
    ("key", "positive"): _key_positive,
    ("key", "positive_mik"): _key_positive_mik,
    ("key", "negative"): _key_negative,
    ("key", "most_common"): _key_most_common_negative,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True)
    # "positive_mik" is the key lane's second, MIK-side ceiling: still a
    # positive control, just answering the OTHER reference, so it gets its
    # own kind rather than overloading "positive" (which stays rekordbox's).
    # "most_common" is the key lane's second named floor alongside
    # "negative" (constant C major): spec section 5 and the round-0 brief
    # name both baselines, so a full round needs both.
    parser.add_argument(
        "--kind", required=True,
        choices=("positive", "positive_mik", "negative", "most_common"),
    )
    parser.add_argument("--fixtures", required=True, help="bundle manifest.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    builder = _BUILDERS.get((args.lane, args.kind))
    if builder is None:
        if (args.lane, args.kind) == ("beatgrid", "negative"):
            raise SystemExit(
                "[controls] the beatgrid negative control is "
                "scripts/beatbench/run_constant_bpm.py, referenced by the lane registry "
                "so the published 0.280 F floor cannot drift. Run that, not this."
            )
        raise SystemExit(
            f"[controls] no {args.kind} control exists for lane {args.lane!r} yet. "
            "It lands with that lane's scorer in the follow-up slice of #1477; "
            "until then the lane cannot post a round, which is the intended behavior."
        )

    payload = builder(Path(args.fixtures))
    payload.update(
        {
            "schema": 1,
            "license": "n/a (control)",
            "shippable": False,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "n_fixtures": len(payload["results"]),
            "n_failed": 0,
        }
    )
    Path(args.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"[controls] {payload['candidate']}: {payload['n_fixtures']} fixtures -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
