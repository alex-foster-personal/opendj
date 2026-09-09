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


_BUILDERS = {("beatgrid", "positive"): _beatgrid_positive}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--kind", required=True, choices=("positive", "negative"))
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
