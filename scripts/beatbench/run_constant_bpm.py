"""Control candidate: a constant-BPM grid that never listens to the audio.

WHY A DELIBERATELY USELESS CANDIDATE BELONGS IN THE TABLE. Round 0 reported
F-measures between 0.30 and 0.86 with no floor to read them against, so
"0.44 on dynamic grids" had no interpretation: it could have been a real
partial success or it could have been what you get for free. This row supplies
the floor. It emits a rigid 128 BPM grid starting at zero, chosen because this
library is heavily concentrated at 120-129 BPM (1410 of 2161 tracks), so it is
the MOST favourable constant a control could pick. Whatever it scores is the
number a candidate has to beat before its score means anything at all.

It is also the phase control. Its grid is correct in period on a large slice of
the library and arbitrary in phase, which is exactly the shape scorer v1.1.0's
raw-versus-shifted F pair was added to distinguish. If the shifted F rescues
THIS candidate as thoroughly as it rescues a real analyzer, the shifted figure
is measuring less than it appears to.

STDLIB ONLY and no model: this must never be able to fail for an environment
reason, or the floor would go missing exactly when a round most needs it.
"""

from __future__ import annotations

import sys

import _harness  # type: ignore[import-not-found]  # sibling of a PEP 723 script

CONTROL_BPM = 128.0


def build_analyzer() -> _harness.Analyzer:
    period_s = 60.0 / CONTROL_BPM

    def analyze(_wav_path: str) -> tuple[list[float], None, float]:
        # The path is ignored ON PURPOSE and the parameter is named to say so:
        # this control's whole value is that it cannot have looked at the audio.
        # The excerpt length is fixed by scripts/beatbench/fixtures.py, and the
        # scorer windows the output anyway, so emitting a generous grid and
        # letting the window trim it is both simpler and honest.
        n_beats = int(120.0 / period_s)
        return [i * period_s for i in range(n_beats)], None, CONTROL_BPM

    return analyze


if __name__ == "__main__":
    args = _harness.build_argparser(__doc__, default_workers=1).parse_args()
    sys.exit(
        _harness.run(
            args,
            candidate="constant_128",
            version=f"control, rigid {CONTROL_BPM} BPM from t=0, audio never read",
            license_note="n/a (control)",
            shippable=False,
            emits_downbeats=False,
            build_analyzer=build_analyzer,
        )
    )
