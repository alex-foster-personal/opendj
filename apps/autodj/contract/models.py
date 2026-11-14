"""Wire models for ``apps.autodj``: closed, frozen, and extra-forbidding.

No planner state crosses this module. Each model is the JSON shape a caller
receives, so a module can move out of process without its callers changing.
"""

from __future__ import annotations

import bisect as _bisect
import json as _json
import math as _math
import statistics as _statistics
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StyleBars(_Wire):
    """The bar range a transition style accepts; ``default`` is used when none is asked."""

    min: int
    max: int
    default: int


class StyleParamsWire(_Wire):
    """AGENT-34: a parametric style's settings, as ``list_styles`` reports them."""

    volume: str
    fade_shape: str  # legal values: ``FADE_SHAPES`` in ``apps.autodj.styles`` (equal_power, linear, smooth, dipped).
    swap_position: str | None
    filter: str | None
    stems: str | None
    min_body_fade_bars: int
    pre_hit_db: float


class StyleTierBars(_Wire):
    """#10144: a style's longest legal length on each grid trust tier; 0 means the tier forbids the style.

    One field per tier (``GRID_TIERS``), so the JSON reads ``{"BAR": n, "BEAT": n, "NONE": n}`` and
    the value cannot be changed in place after validation.
    """

    BAR: int
    BEAT: int
    NONE: int


class StyleSummary(_Wire):
    """One named transition style as an agent reads it (``list_styles``)."""

    name: str
    summary: str
    bars: StyleBars
    needs_beatmatch: bool
    controls: tuple[str, ...]
    when: str
    available: bool
    unavailable_reason: str | None
    # AGENT-34: a parametric style's settings (volume lane, fade_shape, swap_position, filter, stems,
    # min_body_fade_bars, pre_hit_db), null for a fixed style; the Spotify transition it mirrors, or null.
    params: StyleParamsWire | None
    spotify: str | None
    # The tempo mode the style's plans carry: "none" for cut, drop_swap, echo_out and noise_build, else "sync_to_out".
    tempo_mode: str = "sync_to_out"
    # #10144: the longest legal length per grid trust tier (BAR, BEAT, NONE); 0 means the tier forbids the style.
    legal_bars_by_tier: StyleTierBars | None = None


VOCAL_BAR_THRESHOLD = 0.25
ANCHOR_LEAD_BARS = 1.0
STYLE_CAP = 2
VARIETY_WINDOW = 10
SMOOTH = "smooth"
ENERGY_UP = "energy_up"
WIND_DOWN = "wind_down"
SHOWY = "showy"
VOCALS_CLEAN = "vocals_clean"
INTENTS: tuple[str, ...] = (SMOOTH, ENERGY_UP, WIND_DOWN, SHOWY, VOCALS_CLEAN)

#: Gates named on ``rejected`` rows from ``propose_transition`` (spec 05 section 3).
ProposeGate = Literal["vocal", "grid", "length", "tempo", "key", "windows"]
#: Terms in the planner confidence product; ``weakest_factor`` names the lowest.
ConfidenceFactor = Literal["grid", "structure", "vocal", "gap"]
CONFIDENCE_FACTOR_NAMES: tuple[ConfidenceFactor, ...] = ("grid", "structure", "vocal", "gap")
PROPOSE_GATES: tuple[ProposeGate, ...] = ("vocal", "grid", "length", "tempo", "key", "windows")


class ProfileError(ValueError):
    """The documents cannot describe a mixable track."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class PlanError(ValueError):
    """The document is not a transition plan."""

    path: str | None

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class RenderPlanError(ValueError):
    """The plan cannot be rendered as given."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class PairNotFound(LookupError):
    """No transition is saved for this pair."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class Section:
    index: int
    label: str
    raw: str
    start_bar: int
    bars: int
    start_s: float
    end_s: float
    energy: float | None
    bass: float | None
    vocal_bars: int | None

    @property
    def end_bar(self) -> int:
        return self.start_bar + self.bars

    def summary(self) -> dict[str, JsonValue]:
        return {
            "label": self.label,
            "raw": self.raw,
            "start_bar": self.start_bar,
            "bars": self.bars,
            "energy": None if self.energy is None else round(self.energy, 2),
            "bass": None if self.bass is None else round(self.bass, 2),
            "vocal_bars": self.vocal_bars,
        }


@dataclass(frozen=True)
class KeySegment:
    start_bar: int | float | None
    end_bar: int | float | None
    key: str | None
    confidence: float | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "start_bar": self.start_bar,
            "end_bar": self.end_bar,
            "key": self.key,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class HotCue:
    slot: int | str | None
    in_ms: int | float | None
    comment: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {"slot": self.slot, "in_ms": self.in_ms, "comment": self.comment}


@dataclass(frozen=True)
class MikBoundary:
    """One Mixed In Key energy step on the profile bar clock (fractional bar at ``start_s``)."""

    bar: float
    snapped_bar: int | None
    level_before: int
    level_after: int


#: Snap a boundary to the nearest downbeat when it falls within this many bars (one beat in 4/4).
MIK_BOUNDARY_SNAP_BARS = 0.25


GRID_BAR = "BAR"
GRID_BEAT = "BEAT"
GRID_NONE = "NONE"
GRID_TIERS = (GRID_BAR, GRID_BEAT, GRID_NONE)


@dataclass(frozen=True)
class GridTier:
    """How far a grid can be trusted over one span (#10144): BAR, BEAT or NONE, and why when not BAR."""

    tier: str
    reason: str | None = None


@dataclass(frozen=True)
class TrackProfile:
    stable_id: str
    title: str | None
    artist: str | None
    bpm: float | None
    key: str | None
    duration_s: float
    downbeats_s: tuple[float, ...]
    grid_trusted: bool
    grid_note: str | None
    sections: tuple[Section, ...]
    sections_source: str
    bar_energy: tuple[float, ...] | None
    bar_bass: tuple[float, ...] | None
    bar_vocals: tuple[float, ...] | None
    vocals_status: str
    key_segments: tuple[KeySegment, ...] = ()
    hot_cues: tuple[HotCue, ...] = ()
    stems_available: bool | None = None
    #: The deck's stem-set parts (``vocals``, ``drums``, ``bass``, ``other``, ``instrumental``), None when unreported.
    stem_layout: tuple[str, ...] | None = None
    notes: tuple[str, ...] = ()
    lufs: float | None = None
    lufs_reason: str | None = None
    dbtp: float | None = None
    dbtp_reason: str | None = None
    transition_in_bar: int | None = None
    heard_bpm: float | None = None
    deck_pitch_range_pct: float | None = None
    #: Integer key shift applied on deck (for offline render commands); runtime-only, not in ``summary()``.
    heard_key_shift: int = 0
    #: Per-bar mean of the waveform DETAIL mid / high band, each normalized to the track's loudest bar of that
    #: band (0..1, like ``bar_bass``); None when the waveform is mono or absent.
    bar_mid: tuple[float, ...] | None = None
    bar_high: tuple[float, ...] | None = None
    #: MIK levels per bar (majority mapping) and the served MIK scalar; each is None when
    #: ``energy_source`` on the anlz document is ``none``, or when MIK served only the other one.
    mik_bar_level: tuple[int, ...] | None = None
    mik_level: int | None = None
    mik_boundaries: tuple[MikBoundary, ...] = ()
    #: Count of MIK boundaries that could not snap to a downbeat within :data:`MIK_BOUNDARY_SNAP_BARS`.
    mik_grid_suspect: int = 0
    mik_grid_note: str | None = None
    #: #10144: the whole-track grid tier before change points (BAR, BEAT or NONE) and the bars where tempo changes.
    grid_base_tier: str | None = None
    grid_change_bars: tuple[int, ...] = ()

    def grid_tier(self, start_bar: int, bars: int) -> GridTier:
        """The grid trust tier over bars ``start_bar .. start_bar + bars``, with the reason when it is not BAR."""
        base = self.grid_base_tier or (GRID_BAR if self.grid_trusted else GRID_BEAT)
        if base == GRID_NONE:
            return GridTier(GRID_NONE, self.grid_note or "no trackable pulse")
        inside = [bar for bar in self.grid_change_bars if start_bar < bar < start_bar + bars]
        if inside:
            return GridTier(GRID_BEAT, f"change point at bar {inside[0]}")
        if base == GRID_BEAT:
            return GridTier(GRID_BEAT, self.grid_note or "bar phase untrusted")
        return GridTier(GRID_BAR)

    @property
    def judged_bpm(self) -> float | None:
        return self.heard_bpm if self.heard_bpm is not None else self.bpm

    @property
    def bars(self) -> int:
        return len(self.downbeats_s)

    @property
    def bar_s(self) -> float:
        if self.bpm is not None and self.bpm > 0:
            return 240.0 / self.bpm
        if len(self.downbeats_s) >= 2:
            return _statistics.median(b - a for a, b in zip(self.downbeats_s, self.downbeats_s[1:], strict=False))
        raise ProfileError(f"{self.stable_id}: no tempo to size a bar")

    def time_of_bar(self, bar: float) -> float:
        if not self.downbeats_s:
            raise ProfileError(f"{self.stable_id}: no downbeats")
        index = bar - 1.0
        whole = _math.floor(index)
        frac = index - whole
        if whole < 0:
            return self.downbeats_s[0] + index * self.bar_s
        if whole >= len(self.downbeats_s) - 1:
            last = len(self.downbeats_s) - 1
            return self.downbeats_s[last] + (index - last) * self.bar_s
        start = self.downbeats_s[whole]
        return start + frac * (self.downbeats_s[whole + 1] - start)

    def bar_at(self, seconds: float) -> float:
        if not self.downbeats_s:
            raise ProfileError(f"{self.stable_id}: no downbeats")
        first = self.downbeats_s[0]
        if seconds < first:
            return 1.0 + (seconds - first) / self.bar_s
        index = _bisect.bisect_right(self.downbeats_s, seconds) - 1
        if index >= len(self.downbeats_s) - 1:
            last = len(self.downbeats_s) - 1
            return 1.0 + last + (seconds - self.downbeats_s[last]) / self.bar_s
        start, end = self.downbeats_s[index], self.downbeats_s[index + 1]
        return 1.0 + index + (seconds - start) / (end - start)

    def section_at(self, bar: float) -> Section | None:
        return next((section for section in self.sections if section.start_bar <= bar < section.end_bar), None)

    def vocal_bars_between(self, start_bar: int, bars: int) -> int | None:
        if self.bar_vocals is None:
            return None
        lo = max(0, start_bar - 1)
        hi = min(len(self.bar_vocals), start_bar - 1 + bars)
        return sum(1 for value in self.bar_vocals[lo:hi] if value >= VOCAL_BAR_THRESHOLD)

    def mean_between(self, series: tuple[float, ...] | None, start_bar: int, bars: int) -> float | None:
        if series is None:
            return None
        lo = max(0, start_bar - 1)
        hi = min(len(series), start_bar - 1 + bars)
        return None if hi <= lo else sum(series[lo:hi]) / (hi - lo)

    def summary(self) -> dict[str, JsonValue]:
        return {
            "stable_id": self.stable_id,
            "title": self.title,
            "artist": self.artist,
            "bpm": self.bpm,
            "key": self.key,
            "duration_s": round(self.duration_s, 2),
            "bars": self.bars,
            "grid_trusted": self.grid_trusted,
            "grid_note": self.grid_note,
            "sections_source": self.sections_source,
            "sections": [section.summary() for section in self.sections],
            "vocals_status": self.vocals_status,
            "key_segments": [segment.to_json() for segment in self.key_segments],
            "hot_cues": [cue.to_json() for cue in self.hot_cues],
            "stems_available": self.stems_available,
            "loudness": {
                "lufs": self.lufs,
                "reason": self.lufs_reason,
                "dbtp": self.dbtp,
                **({"dbtp_reason": self.dbtp_reason} if self.dbtp_reason is not None else {}),
            },
            "transition_in_bar": self.transition_in_bar,
            "notes": list(self.notes),
        }

    def to_json(self) -> dict[str, JsonValue]:
        """The summary plus the grid and the per-bar series (``summary()`` stays compact)."""

        def series(values: tuple[float, ...] | None) -> JsonValue:
            return None if values is None else list(values)

        return {
            **self.summary(),
            "downbeats_s": list(self.downbeats_s),
            "bar_energy": series(self.bar_energy),
            "bar_bass": series(self.bar_bass),
            "bar_mid": series(self.bar_mid),
            "bar_high": series(self.bar_high),
            "bar_vocals": series(self.bar_vocals),
        }


@dataclass(frozen=True)
class Side:
    deck: int
    track: str
    start_bar: int
    bars: int
    trim_db: float | None = None
    trim_reason: str | None = None
    #: #10142: the incoming deck's static key shift (semitones, -3..3) and key lock; the outgoing side keeps defaults.
    key_shift_semitones: int = 0
    keylock: bool = True


@dataclass(frozen=True)
class PlanEvent:
    at_bar: float
    deck: int | None
    kind: str
    name: str
    value: float | None = None
    over_bars: float = 0.0
    #: The field of a multi-value control (fx_noise: ``level`` or ``sweep``); None for every scalar control.
    field: str | None = None

    def target(self) -> float:
        value = self.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not _math.isfinite(value):
            raise ValueError(f"{self.kind} {self.name} at bar {self.at_bar:g} has no numeric value: {value!r}")
        return float(value)

    def to_json(self) -> dict[str, JsonValue]:
        document: dict[str, JsonValue] = {"at_bar": self.at_bar, "deck": self.deck}
        if self.kind == "do":
            document["do"] = self.name
        else:
            if self.field is None:
                document[self.kind] = {self.name: self.value}
            else:
                document[self.kind] = {self.name: {self.field: self.value}}
            if self.kind == "ramp":
                document["over_bars"] = self.over_bars
        return document


@dataclass(frozen=True)
class BassEntry:
    in_bar: int | None
    entry_plan_bar: float | None
    hit_plan_bar: float
    note: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "in_bar": self.in_bar,
            "entry_plan_bar": self.entry_plan_bar,
            "hit_plan_bar": self.hit_plan_bar,
            "note": self.note,
        }


@dataclass(frozen=True)
class TransitionPlan:
    out: Side
    in_: Side
    style: str
    events: tuple[PlanEvent, ...]
    tempo_mode: str = "sync_to_out"
    anchor: str = "window"
    # #10148: a glide_to_in plan's glide length in bars (1..bars); None on every other mode.
    tempo_over_bars: int | None = None
    intent: str | None = None
    rationale: str | None = None
    bass_entry: BassEntry | None = None
    shape: str | None = None
    extra_json: str = "{}"

    @property
    def extra(self) -> dict[str, JsonValue]:
        """Extension keys as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.extra_json)

    @property
    def bars(self) -> int:
        return self.out.bars

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "out": {
                "deck": self.out.deck,
                "track": self.out.track,
                "window": {"start_bar": self.out.start_bar, "bars": self.out.bars},
            },
            "in": {
                "deck": self.in_.deck,
                "track": self.in_.track,
                "window": {"start_bar": self.in_.start_bar, "bars": self.in_.bars},
                "trim_db": self.in_.trim_db,
                "trim_reason": self.in_.trim_reason,
                # #10142: only when not the default, so every existing plan prints byte-identical.
                **({"key_shift_semitones": self.in_.key_shift_semitones} if self.in_.key_shift_semitones else {}),
                **({} if self.in_.keylock else {"keylock": False}),
            },
            "anchor": self.anchor,
            "style": self.style,
            "tempo": (
                {"mode": self.tempo_mode}
                if self.tempo_over_bars is None
                else {"mode": self.tempo_mode, "over_bars": self.tempo_over_bars}
            ),
            "events": [event.to_json() for event in self.events],
            "intent": self.intent,
            "rationale": self.rationale,
            "bass_entry": None if self.bass_entry is None else self.bass_entry.to_json(),
            "shape": self.shape,
        }

    def to_document(self) -> dict[str, JsonValue]:
        return {**self.extra, **self.to_json()}


@dataclass(frozen=True)
class ValidationResult:
    """The validation information the transition-score domain consumes."""

    ok: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class RenderLayout:
    start_out_bar: float
    start_out_s: float
    in_ratio: float
    in_fold: float
    end_in_bar: float
    out_bar_s: float
    #: Heard outgoing tempo / stored bar clock (1.0 when no pitch).
    out_ratio: float = 1.0

    def out_downbeats_render_s(self, out_profile: TrackProfile, until_s: float) -> list[float]:
        downs = [
            (d - self.start_out_s) / self.out_ratio for d in out_profile.downbeats_s if d >= self.start_out_s - 1e-9
        ]
        if not downs:
            raise RenderPlanError(f"{out_profile.stable_id}: no downbeat at or after the render's start")
        step = self.out_bar_s / self.out_ratio
        while downs[-1] + step <= until_s + 1e-9:
            downs.append(downs[-1] + step)
        return [d for d in downs if d <= until_s + 1e-9]


@dataclass(frozen=True)
class ChooserCandidate:
    cid: str
    style: str
    m: float
    vetoed: bool


@dataclass(frozen=True)
class PairTransition:
    out_track_id: str
    in_track_id: str
    plan_json: str
    source: str
    style: str
    created_at: str
    updated_at: str
    times_played: int

    @property
    def plan(self) -> dict[str, JsonValue]:
        """The saved plan as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.plan_json)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "plan": self.plan,
            "meta": {
                "out_track_id": self.out_track_id,
                "in_track_id": self.in_track_id,
                "source": self.source,
                "style": self.style,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "times_played": self.times_played,
            },
        }
