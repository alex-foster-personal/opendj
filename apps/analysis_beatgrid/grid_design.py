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

THE OFFSET MOVED WITH THE TIME BASE (JIK, Fri 2 Oct 2026, "Shift at import").
The +15 ms above was fitted against rekordbox PQTZ times read as stored,
which start at the first sample of a RAW decode, while the bench fixtures
were decoded by ffmpeg, which trims an MP3's encoder lead-in (1105 samples,
25.06 ms at 44.1 kHz) as every engine of ours does. Measured Thu 1 Oct 2026 on
32 library MP3s (rekordbox time zero = raw decode on 30). So on a tagged MP3,
about 89% of the library, +15 ms put our beats where rekordbox's beats are in
rekordbox's time base, not ours. rekordbox positions are now shifted onto our
timeline at import (`apps.shared.mp3_lead_in`), so serving the same placement
relative to rekordbox takes 15 - 25.06 = -10 ms (rounded). That is a
translation of the 29 Sep fit, not a new fit: the KPIs below were measured at
+15 against unshifted references, which is the same relative placement on
those tracks. A round rebuilt through the daemon reads `/anlz`, which now
serves shifted references, so the next round measures -10 directly. Untagged
MP3s and other formats had no time-base gap, so on them -10 sits 25 ms
earlier against rekordbox than +15 did; the round that measures this should
split its rows by `apps.shared.mp3_lead_in.read_lead_in`.

KPIs. `KPIS` names the bench metric each target is read from, the value
measured at the served design, and the v2 target. The targets are proposals
set with the offset decision, not measurements. The measured values are
checked against the committed `KPI_ROUND` results.json by
`tests/analysis_beatgrid/test_grid_design.py`, so a re-scored round that moves
a number fails there until this table is updated with it. Round 6 (the
every-downbeat bar-phase vote, `bar_phase.vote_bar_phase`) moved two: F
0.837 -> 0.851 because 3 more fixed tracks are gridded instead of failing
closed, and downbeat 72.6 -> 73.8 percent.

THE DOWNBEAT DENOMINATOR. The scorer's downbeat mean skips tracks the grid
failed closed on (193 of 200 fixed in round 6, 190 in round 4). Counting
those as 0, the same rows read 71.2 percent (round 4: 69.0), and 137 of 200
fixed tracks land every bar-1 on rekordbox's (round 4: 133).

-Claude
"""

from __future__ import annotations

from dataclasses import dataclass

#: Shift added to every regularized beat, as served. Matches rekordbox on our
#: (lead-in trimmed) timeline: the 29 Sep +15 ms less the 25.06 ms MP3 lead-in.
OFFSET_SERVED_S = -0.010

#: The v2 target: human-annotated beat position (round 2, GTZAN median).
OFFSET_V2_TARGET_S = 0.008

#: The bench round the KPIs below were measured in, and its served row.
KPI_ROUND = "ops/beatbench/round-6"
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
        measured=0.851,
        v2_target=0.9,
        higher_is_better=True,
        reference="rekordbox PQTZ",
    ),
    Kpi(
        name="fixed-tempo downbeat agreement vs rekordbox (%)",
        partition="fixed",
        metric="downbeat_agreement_mean",
        measured=73.8,
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
