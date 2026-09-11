"""The eleven PARITY-01 lanes, named from what rekordbox holds.

Beatgrid is owned by BEATMAP-01. This module records that ownership so a
PARITY-01 round cannot grow a second beat metric by accident.
"""

from __future__ import annotations

from typing import Final

BEATMAP_OWNER: Final = (
    "BEATMAP-01 (apps.analysis_bench.scorers.beatgrid; "
    "was scripts/beatbench/scorer.py)"
)

LANE_IDS: Final[tuple[str, ...]] = (
    "bpm",
    "key",
    "beatgrid",
    "downbeat",
    "waveform_preview",
    "waveform_detail",
    "waveform_triband",
    "phrase",
    "cues_db",
    "cues_anlz",
    "vocal",
)

SCORED_THIS_ROUND: Final[tuple[str, ...]] = (
    "bpm",
    "key",
    "phrase",
    "cues_db",
    "cues_anlz",
)
DELEGATED_THIS_ROUND: Final[tuple[str, ...]] = ("beatgrid", "downbeat")

# Follow-up issue bodies start `Part k of 5 of #1520`. This PR is part 1.
REMAINING_REASON: Final[dict[str, str]] = {
    "waveform_preview": (
        "Part 2 of 5 of #1520: waveform preview (ANLZ PWAV/PWV2). "
        "No own waveform analysis exists yet."
    ),
    "waveform_detail": (
        "Part 2 of 5 of #1520: waveform detail (ANLZ PWV3/PWV4/PWV5). "
        "Unreadable .EXT siblings are ungradable, never a miss."
    ),
    "waveform_triband": (
        "Part 2 of 5 of #1520: waveform tri-band (ANLZ PWV6/PWV7). "
        "No own waveform analysis exists yet."
    ),
    "vocal": (
        "Part 5 of 5 of #1520: vocal (ANLZ PVDI). "
        "Tracks with no PVDI fourcc are ungradable."
    ),
}

DENOMINATOR_NAME: Final[dict[str, str]] = {
    "bpm": "tracks with rekordbox BPM (djmdContent.BPM x100) in this fixture",
    "key": (
        "tracks with rekordbox key (djmdKey.ScaleName, not sentinel All) "
        "in this fixture"
    ),
    "beatgrid": "delegated to BEATMAP-01; not a PARITY-01 denominator",
    "downbeat": "delegated to BEATMAP-01; not a PARITY-01 denominator",
    "waveform_preview": (
        "tracks with rekordbox waveform preview (PWAV/PWV2) in this fixture"
    ),
    "waveform_detail": (
        "tracks with readable rekordbox color waveform "
        "(.EXT PWV3/PWV4/PWV5) in this fixture"
    ),
    "waveform_triband": (
        "tracks with rekordbox tri-band waveform (PWV6/PWV7) in this fixture"
    ),
    "phrase": "tracks with rekordbox PSSI in this fixture",
    "cues_db": "tracks with at least one djmdCue row in this fixture",
    "cues_anlz": (
        "tracks with readable rekordbox ANLZ cues (PCOB/PCO2) in this fixture"
    ),
    "vocal": "tracks with rekordbox PVDI in this fixture",
}
