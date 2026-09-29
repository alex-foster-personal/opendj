"""Beatgrid design config: the product choices the grid fit serves, and their KPIs.

ONE PLACE FOR DECISIONS, NOT FOR ARITHMETIC. `grid_fit` holds the fitting
rules (outlier cut, rounding steps), which follow from the data. The values
here are product calls someone made, so each names who made it and when, and
a change to any of them is a decision to record rather than a tuning knob.

THE OFFSET (JIK, Tue 29 Sep 2026). Beat This! beats sit early. Two references
disagree on how much:

  * rekordbox places beats about 12-15 ms after them. Round 4
    (`ops/beatbench/round-4/`), fixed-tempo, per-track signed shift of our
    fitted line against rekordbox: median -12.2 ms unshifted, -4.6 ms at
    +8 ms, +2.0 ms at +15 ms. SERVED NOW, because v1 grids are compared
    against rekordbox on the user's own library.
  * Human annotation places beats about 8 ms after them (round 2, GTZAN, 998
    clips, median). That is the TARGET FOR V2, when our grids stand on their
    own rather than next to rekordbox's.

KPIs. `KPIS` names the round-4 metric each target is read from, the value
measured at the served design, and the v2 target. The targets are proposals
set with the offset decision, not measurements. The measured values are
checked against the committed `ops/beatbench/round-4/results.json` by
`tests/analysis_beatgrid/test_grid_design.py`, so a re-scored round that moves
a number fails there until this table is updated with it.

-Claude
"""

from __future__ import annotations

from dataclasses import dataclass

#: Shift added to every regularized beat, as served. Matches rekordbox.
OFFSET_SERVED_S = 0.015

#: The v2 target: human-annotated beat position (round 2, GTZAN median).
OFFSET_V2_TARGET_S = 0.008

#: The bench round the KPIs below were measured in, and its served row.
KPI_ROUND = "ops/beatbench/round-4"
KPI_SERVED_CANDIDATE = "line_round_offset"


@dataclass(frozen=True)
class Kpi:
    """One beatgrid KPI: where it is read, what the served design measured, the goal."""

    name: str
    #: `results.json` path: candidate partition (`fixed` or `dynamic`), then metric key.
    partition: str
    metric: str
    #: Value measured for `KPI_SERVED_CANDIDATE` in `KPI_ROUND`.
    measured: float
    #: The v2 goal, and whether higher or lower is better.
    v2_target: float
    higher_is_better: bool
    reference: str


KPIS: tuple[Kpi, ...] = (
    Kpi(
        name="fixed-tempo BPM exact to 0.01 vs rekordbox (%)",
        partition="fixed",
        metric="bpm_exact_0_01_pct",
        measured=76.5,
        v2_target=90.0,
        higher_is_better=True,
        reference="rekordbox PQTZ",
    ),
    Kpi(
        name="fixed-tempo whole-grid shift vs rekordbox, median |shift| (ms)",
        partition="fixed",
        metric="abs_global_shift_p50_ms",
        measured=7.71,
        v2_target=5.0,
        higher_is_better=False,
        reference="rekordbox PQTZ",
    ),
    Kpi(
        name="fixed-tempo F-measure at 70 ms vs rekordbox",
        partition="fixed",
        metric="f_measure_mean",
        measured=0.837,
        v2_target=0.9,
        higher_is_better=True,
        reference="rekordbox PQTZ",
    ),
    Kpi(
        name="fixed-tempo downbeat agreement vs rekordbox (%)",
        partition="fixed",
        metric="downbeat_agreement_mean",
        measured=72.6,
        v2_target=85.0,
        higher_is_better=True,
        reference="rekordbox PQTZ",
    ),
)

__all__ = [
    "KPIS",
    "KPI_ROUND",
    "KPI_SERVED_CANDIDATE",
    "OFFSET_SERVED_S",
    "OFFSET_V2_TARGET_S",
    "Kpi",
]
