"""MIK energy seam block for try-er candidates (issue #10132, spec 17 section 6).

Builds the structured energy line and strip payload for one rendered plan. When
either track has no ``mik_bar_level`` series the block is unscored (AGENT-33):
never a relative within-track seam.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from apps.autodj.contract.models import ENERGY_UP, SHOWY, SMOOTH, VOCALS_CLEAN, WIND_DOWN, TrackProfile, TransitionPlan

GRID_WARNING = "MIK boundaries sit half a bar off this grid"
UNSCORED_LINE = "energy unscored (no MIK series)"
UNSCORED_REASON = "no MIK series"
UNSCORED_ENERGY_JSON: dict[str, Any] = {
    "line": UNSCORED_LINE,
    "numbers": [],
    "unscored": True,
    "mik_grid_suspect": False,
    "grid_warning": None,
    "out": None,
    "in": None,
    "overlap_out_start_bar": None,
    "overlap_out_end_bar": None,
    "overlap_in_start_bar": None,
    "overlap_in_end_bar": None,
    "boundaries": [],
}


@dataclass(frozen=True)
class EnergyNumber:
    value: str
    title: str


@dataclass(frozen=True)
class EnergyStripSide:
    start_bar: int
    levels: tuple[int, ...]


@dataclass(frozen=True)
class EnergyStripBoundary:
    bar: float
    deck: Literal["out", "in"]


@dataclass(frozen=True)
class EnergyBlock:
    line: str
    numbers: tuple[EnergyNumber, ...]
    unscored: bool
    mik_grid_suspect: bool
    grid_warning: str | None
    out: EnergyStripSide | None
    in_: EnergyStripSide | None
    overlap_out_start_bar: int | None
    overlap_out_end_bar: int | None
    overlap_in_start_bar: int | None
    overlap_in_end_bar: int | None
    boundaries: tuple[EnergyStripBoundary, ...]

    def to_json(self) -> dict[str, Any]:
        document = asdict(self)
        document["in"] = None if self.in_ is None else asdict(self.in_)
        document.pop("in_")
        document["numbers"] = [asdict(number) for number in self.numbers]
        document["boundaries"] = [asdict(boundary) for boundary in self.boundaries]
        if self.out is not None:
            document["out"] = asdict(self.out)
        return document


def _intent_band(intent: str | None) -> tuple[int, int] | None:
    if intent in (None, SMOOTH, VOCALS_CLEAN):
        return (-1, 1)
    if intent == ENERGY_UP:
        return (1, 2)
    if intent == WIND_DOWN:
        return (-2, -1)
    if intent == SHOWY:
        return None
    return (-1, 1)


def _band_label(band: tuple[int, int]) -> str:
    lo, hi = band
    if lo == hi:
        return f"{lo:+d}" if lo else "0"
    return f"{lo:+d}..{hi:+d}"


def _band_distance(step: int, band: tuple[int, int]) -> int:
    lo, hi = band
    if lo <= step <= hi:
        return 0
    if step < lo:
        return lo - step
    return step - hi


def _verdict(step: int, band: tuple[int, int] | None) -> str:
    if band is None:
        return "n/a"
    return "fits" if _band_distance(step, band) == 0 else "miss"


def _level_at(profile: TrackProfile, bar: int) -> int | None:
    levels = profile.mik_bar_level
    if levels is None:
        return None
    index = bar - 1
    if index < 0 or index >= len(levels):
        return None
    return levels[index]


def _window_levels(profile: TrackProfile, start_bar: int, bars: int) -> tuple[int, ...] | None:
    if profile.mik_bar_level is None:
        return None
    end = start_bar + bars - 1
    levels: list[int] = []
    for bar in range(start_bar, end + 1):
        level = _level_at(profile, bar)
        if level is None:
            return None
        levels.append(level)
    return tuple(levels)


def _boundaries_in(
    profile: TrackProfile,
    deck: Literal["out", "in"],
    start_bar: int,
    end_bar: int,
) -> tuple[EnergyStripBoundary, ...]:
    found: list[EnergyStripBoundary] = []
    for boundary in profile.mik_boundaries:
        bar = boundary.bar if boundary.snapped_bar is None else float(boundary.snapped_bar)
        if start_bar <= bar <= end_bar:
            found.append(EnergyStripBoundary(bar=bar, deck=deck))
    return tuple(found)


def _unscored() -> EnergyBlock:
    return EnergyBlock(
        line=UNSCORED_LINE,
        numbers=(),
        unscored=True,
        mik_grid_suspect=False,
        grid_warning=None,
        out=None,
        in_=None,
        overlap_out_start_bar=None,
        overlap_out_end_bar=None,
        overlap_in_start_bar=None,
        overlap_in_end_bar=None,
        boundaries=(),
    )


def build_energy_block(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    *,
    intent: str | None,
) -> EnergyBlock:
    """Energy line and strip for one plan; unscored when either side lacks a MIK bar series."""
    overlap = plan.bars
    if overlap < 1:
        return _unscored()
    if out_profile.mik_bar_level is None or in_profile.mik_bar_level is None:
        return _unscored()

    out_track_level = out_profile.mik_level
    in_track_level = in_profile.mik_level
    if out_track_level is None or in_track_level is None:
        return _unscored()

    out_seam_bar = plan.out.start_bar + overlap - 1
    in_seam_bar = plan.in_.start_bar + overlap
    seam_out = _level_at(out_profile, out_seam_bar)
    seam_in = _level_at(in_profile, in_seam_bar)
    if seam_out is None or seam_in is None:
        return _unscored()

    seam_step = seam_in - seam_out
    intent_label = intent or SMOOTH
    band = _intent_band(intent)
    band_text = "any" if band is None else _band_label(band)
    verdict = _verdict(seam_step, band)
    line = (
        f"energy {out_track_level} -> {in_track_level} (MIK track levels)   "
        f"seam {seam_out} -> {seam_in} at in bar {in_seam_bar}   "
        f"intent {intent_label} wants {band_text}: {verdict}"
    )

    numbers = (
        EnergyNumber(str(out_track_level), "MIK scalar on the outgoing track (track_fields.energy)"),
        EnergyNumber(str(in_track_level), "MIK scalar on the incoming track (track_fields.energy)"),
        EnergyNumber(str(seam_out), f"MIK level at outgoing bar {out_seam_bar} (last overlapping bar)"),
        EnergyNumber(str(seam_in), f"MIK level at incoming bar {in_seam_bar} (landing bar)"),
        EnergyNumber(str(in_seam_bar), "Incoming landing bar on the beatgrid"),
    )

    out_levels = _window_levels(out_profile, plan.out.start_bar, plan.out.bars)
    in_levels = _window_levels(in_profile, plan.in_.start_bar, plan.in_.bars)
    if out_levels is None or in_levels is None:
        return _unscored()

    overlap_out_start = plan.out.start_bar
    overlap_out_end = out_seam_bar
    overlap_in_start = plan.in_.start_bar
    overlap_in_end = in_seam_bar

    suspect = out_profile.mik_grid_suspect or in_profile.mik_grid_suspect
    grid_warning = GRID_WARNING if suspect else None
    boundaries = (
        *_boundaries_in(out_profile, "out", plan.out.start_bar, overlap_out_end),
        *_boundaries_in(in_profile, "in", plan.in_.start_bar, overlap_in_end),
    )

    return EnergyBlock(
        line=line,
        numbers=numbers,
        unscored=False,
        mik_grid_suspect=suspect,
        grid_warning=grid_warning,
        out=EnergyStripSide(plan.out.start_bar, out_levels),
        in_=EnergyStripSide(plan.in_.start_bar, in_levels),
        overlap_out_start_bar=overlap_out_start,
        overlap_out_end_bar=overlap_out_end,
        overlap_in_start_bar=overlap_in_start,
        overlap_in_end_bar=overlap_in_end,
        boundaries=boundaries,
    )
