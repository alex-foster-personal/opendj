"""Auto gain: the per-track playback gain offset that matches loudness (AUTOGAIN-01..06).

Mini-PRD
--------
R1 done  Offset = target LUFS minus the track's measured integrated LUFS
         (ITU-R BS.1770 / EBU R128), clamped to ``OFFSET_MIN_DB..OFFSET_MAX_DB``.
R2 done  A boost never pushes the track's measured true peak above
         ``TRUE_PEAK_CEILING_DBTP``. A cut is never limited, and the true-peak
         rule never turns a wanted boost into a cut.
R3 done  A track with no loudness analysis gets 0 dB and the explicit status
         ``not_analysed``; nothing is guessed.
R4 done  A user-set offset sits beside the analysed one and wins. It is applied
         as set (range-checked, not peak-limited: the user chose it).
R5 done  Auto gain switched off is 0 dB with status ``off``, whatever the
         analysis or the user value says.

Acceptance
----------
[if] a -6 LUFS track meets a -12 LUFS target [then] offset is -6 dB [else failed]
[if] a -30 LUFS track asks for +18 dB [then] it is clamped to OFFSET_MAX_DB [else failed]
[if] a -15 LUFS track peaks at -2 dBTP [then] the +3 dB boost is cut to +1 dB [else failed]
[if] loudness is missing [then] status is not_analysed and offset 0 dB [else failed]
[if] the user set -2 dB [then] effective is -2 dB whatever the analysis [else failed]

Pure and stdlib-only: the delivery layers call :func:`compute_auto_gain` and
own every read of prefs, analysis and the user value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

#: Default target. rekordbox's own reference is not documented by AlphaTheta;
#: -12 LUFS was chosen by measurement over the 2,998 analysed tracks in the
#: library: it leaves a 0.3 LU p10-p90 spread with the true-peak rule on, where
#: -9 LUFS (the target third parties report for rekordbox) leaves 3.3 LU,
#: because most quiet masters have no headroom to be boosted into.
DEFAULT_TARGET_LUFS: Final[float] = -12.0
TARGET_MIN_LUFS: Final[float] = -24.0
TARGET_MAX_LUFS: Final[float] = -5.0
DEFAULT_ENABLED: Final[bool] = True

#: Clamp on any offset, analysed or user-set.
OFFSET_MIN_DB: Final[float] = -12.0
OFFSET_MAX_DB: Final[float] = 12.0

#: A boost may lift the measured true peak to this and no further. -1 dBTP is
#: the EBU R128 / streaming-platform ceiling for intersample peaks.
TRUE_PEAK_CEILING_DBTP: Final[float] = -1.0

AutoGainStatus = Literal["analysed", "user", "not_analysed", "off"]
LimitedBy = Literal["range", "true_peak"]


@dataclass(frozen=True)
class AutoGain:
    """What auto gain does to one track, and why."""

    status: AutoGainStatus
    enabled: bool
    target_lufs: float
    integrated_lufs: float | None
    true_peak_dbtp: float | None
    #: The offset the analysis asks for, after clamp and true-peak limit.
    analysed_offset_db: float | None
    user_offset_db: float | None
    #: The offset playback applies (0 when off or not analysed).
    effective_offset_db: float
    limited_by: LimitedBy | None

    @property
    def linear_gain(self) -> float:
        return float(10.0 ** (self.effective_offset_db / 20.0))


def validate_target_lufs(value: float) -> float:
    if not math.isfinite(value) or not TARGET_MIN_LUFS <= value <= TARGET_MAX_LUFS:
        raise ValueError(
            f"auto gain target must be {TARGET_MIN_LUFS}..{TARGET_MAX_LUFS} LUFS, got {value}"
        )
    return float(value)


def validate_user_offset_db(value: float) -> float:
    if not math.isfinite(value) or not OFFSET_MIN_DB <= value <= OFFSET_MAX_DB:
        raise ValueError(
            f"auto gain user offset must be {OFFSET_MIN_DB}..{OFFSET_MAX_DB} dB, got {value}"
        )
    return float(value)


def _analysed_offset(
    integrated_lufs: float, true_peak_dbtp: float | None, target_lufs: float
) -> tuple[float, LimitedBy | None]:
    wanted = target_lufs - integrated_lufs
    offset = min(OFFSET_MAX_DB, max(OFFSET_MIN_DB, wanted))
    limited: LimitedBy | None = "range" if offset != wanted else None
    if offset > 0 and true_peak_dbtp is not None:
        headroom = max(0.0, TRUE_PEAK_CEILING_DBTP - true_peak_dbtp)
        if offset > headroom:
            offset, limited = headroom, "true_peak"
    elif offset > 0:
        # No true peak measured: no proof a boost is safe, so do not boost.
        offset, limited = 0.0, "true_peak"
    return offset, limited


def compute_auto_gain(
    *,
    enabled: bool,
    target_lufs: float,
    integrated_lufs: float | None,
    true_peak_dbtp: float | None,
    user_offset_db: float | None,
) -> AutoGain:
    """Resolve one track's auto gain. Raises on an out-of-range input."""
    validate_target_lufs(target_lufs)
    if user_offset_db is not None:
        validate_user_offset_db(user_offset_db)
    measured = integrated_lufs is not None and math.isfinite(integrated_lufs)
    peak = true_peak_dbtp if true_peak_dbtp is not None and math.isfinite(true_peak_dbtp) else None
    analysed: float | None = None
    limited: LimitedBy | None = None
    if measured:
        assert integrated_lufs is not None
        analysed, limited = _analysed_offset(integrated_lufs, peak, target_lufs)
    status: AutoGainStatus
    if not enabled:
        status, effective = "off", 0.0
    elif user_offset_db is not None:
        status, effective = "user", user_offset_db
    elif analysed is not None:
        status, effective = "analysed", analysed
    else:
        status, effective = "not_analysed", 0.0
    return AutoGain(
        status=status,
        enabled=enabled,
        target_lufs=target_lufs,
        integrated_lufs=integrated_lufs if measured else None,
        true_peak_dbtp=peak,
        analysed_offset_db=analysed,
        user_offset_db=user_offset_db,
        effective_offset_db=effective,
        limited_by=limited if status == "analysed" else None,
    )


__all__ = [
    "DEFAULT_ENABLED",
    "DEFAULT_TARGET_LUFS",
    "OFFSET_MAX_DB",
    "OFFSET_MIN_DB",
    "TARGET_MAX_LUFS",
    "TARGET_MIN_LUFS",
    "TRUE_PEAK_CEILING_DBTP",
    "AutoGain",
    "compute_auto_gain",
    "validate_target_lufs",
    "validate_user_offset_db",
]
