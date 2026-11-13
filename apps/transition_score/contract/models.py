"""The try-er contract: what the execution layer hands the endpoint (TRYER-03).

``try_pair`` proposes N candidate transitions for one pair, renders each
offline through ``odj-audio render --plan --decks-out``, scores each from the
rendered audio and returns them ranked. The endpoint (``/api/v1/dj/try``,
``opendj dj try``, MCP ``try_transitions``) imports these types from
``apps.transition_score.contract``.

A candidate's ``score`` is the macro measure M (file 15): ``0.0`` when a veto
fired (named in ``vetoes``), ``None`` when fewer than
:data:`MIN_CORE_MEASURED` of the core factors could be measured, otherwise the
weighted geometric mean of the measured layer-1 factors. ``unknown`` names
every factor that was not measured and why; it is never read as a pass.
"""

from __future__ import annotations

import collections.abc as _abc
import json as _json
from dataclasses import dataclass, field

from pydantic import JsonValue

#: Pinned per round (spec ``transitions-tryer.md`` section 5). 0.2.0 = round 1 (kick-only template, dominant-peak gate);
#: 0.2.1 = round 2 (cut timing held to the downbeat as rendered);
#: 0.3.0 = round 6 (outgoing kick must dominate its own beat; alignment per overlap quarter);
#: 0.4.0 = round 7 (coverage rule: 16 timed beats and 2 in at least 3 of 4 quarters);
#: 0.5.0 = round 10 (a beat whose kick band is absent is timed on the 2-8 kHz band);
#: 0.6.0 = round 14 (loudness_continuity weight 0.4; loudness_loss veto, wind-down exempt);
#: 0.7.0 = round 15 (bass_overlap counts beats with both lows near full; low_continuity factor; true peak vs the louder deck).
#: 0.7.1 = low_hole's switch ignores beats with no bass on either deck and falls back to the plan's bass hit.
#: Versioning rule: ADR-NEW-transition-quality-measure.
#: 0.8.0 = round 22: true peak is reported only (hygiene, the maintainer never heard an over), no veto and no weight.
#: 0.9.0 = round 23: bass_overlap is unknown when either solo's 20-150 Hz bar-median is below BASS_REF_PRESENT_DB.
#: 0.10.0 = #10187: band_continuity_low/mid/high, the mix's short-term band loudness (engine shelves 250 Hz /
#: 5 kHz) under the outgoing at unity from the overlap start to the handover, knee 1.5 LU, weight 0.1 each, not core.
#: 0.11.0 = #10182 (round 25): momentary_hole (400 ms series under its same-position neighbours, held 300 ms, free
#: 1.5 LU, knee 3 LU, weight 0.1, not core); bass_overlap graded by depth (bass_overlap_depth_db, knee 3 dB-beats);
#: seam_clicks also scans the whole overlap (overlap_clicks).
#: 0.12.0 = #12477: vocal_cut diagnostic (outgoing vocal active in the VOCAL_CUT_WINDOW_S before the outgoing deck
#: falls VOCAL_CUT_GAIN_DB under its pre-overlap level); reported only, no factor, no veto; unknown without a vocal stem.
SCORER_VERSION = "0.12.0"
#: #12477 (spec 10 section 3): the window before the gain crossing that is checked for an active outgoing vocal.
VOCAL_CUT_WINDOW_S = 0.5
#: #12477: the outgoing deck counts as inaudible once its level is this far under its pre-overlap level.
VOCAL_CUT_GAIN_DB = -20.0
#: The core factors ``measured`` counts (k of n); a composite needs at least
#: :data:`MIN_CORE_MEASURED` of them (spec 10 section 3).
CORE_FACTORS: tuple[str, ...] = (
    "alignment",
    "downbeat_phase",
    "loudness_continuity",
    "bass_overlap",
    "vocal_overlap",
    "cut_to_downbeat",
    "seam_clicks",
)
MIN_CORE_MEASURED = 4


@dataclass(frozen=True)
class Veto:
    """A layer-0 check that fired: M is 0 and this names why."""

    name: str
    value: float
    threshold: float

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "value": self.value, "threshold": self.threshold}


@dataclass(frozen=True)
class Measured:
    """How many of the core factors were measured (k of n)."""

    k: int
    n: int

    def to_json(self) -> dict[str, int]:
        return {"k": self.k, "n": self.n}


def core_measured(factors: _abc.Mapping[str, float]) -> Measured:
    """How many core layer-1 factors were measured in ``factors`` (k of n)."""
    return Measured(k=sum(1 for name in CORE_FACTORS if name in factors), n=len(CORE_FACTORS))


@dataclass(frozen=True)
class ScoreResult:
    """The scorer's verdict on one render."""

    metrics_json: str
    factors_json: str
    vetoes: tuple[Veto, ...]
    measured: Measured
    unknown_json: str
    score: float | None
    scorer_version: str = SCORER_VERSION

    @property
    def metrics(self) -> dict[str, JsonValue]:
        """The measured metrics as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.metrics_json)

    @property
    def factors(self) -> dict[str, float]:
        """The layer-1 factors as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.factors_json)

    @property
    def unknown(self) -> dict[str, str]:
        """Each unmeasured factor and why, as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.unknown_json)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "metrics": self.metrics,
            "factors": self.factors,
            "vetoes": [v.to_json() for v in self.vetoes],
            "measured": self.measured.to_json(),
            "unknown": self.unknown,
            "score": self.score,
            "scorer_version": self.scorer_version,
        }


DEFAULT_ENERGY_JSON = _json.dumps(
    {
        "line": "energy unscored (no MIK series)",
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
)


@dataclass(frozen=True)
class Candidate:
    """One rendered and scored transition plan."""

    id: str
    plan_json: str
    plan_sha256: str
    style: str
    bars: int
    metrics_json: str
    factors_json: str
    vetoes: tuple[Veto, ...]
    measured: Measured
    unknown_json: str
    score: float | None
    scorer_version: str
    #: The renderer's wall time in this try; None for a refused candidate (no render to time).
    render_ms: float | None
    score_ms: float
    #: Why this candidate could not be rendered or scored, or None. A refused candidate is kept, unscored, so a
    #: caller sees every proposal; it ranks below every other and is never ``best_candidate``.
    refused: str | None = None
    #: The kept 16-bit mix (``try_plans(keep_renders_in=...)``), or None when the caller kept no renders.
    render_path: str | None = None
    #: Planner confidence from ``propose_transition`` for this plan (issue #10150).
    planner_confidence: float | None = None
    planner_weakest_factor: str | None = None
    planner_confidence_title: str | None = None
    #: MIK energy seam line and strip for the open review page (issue #10132).
    energy_json: str = DEFAULT_ENERGY_JSON

    @property
    def plan(self) -> dict[str, JsonValue]:
        """The recorded plan as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.plan_json)

    @property
    def metrics(self) -> dict[str, JsonValue]:
        """The measured metrics as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.metrics_json)

    @property
    def factors(self) -> dict[str, float]:
        """The layer-1 factors as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.factors_json)

    @property
    def unknown(self) -> dict[str, str]:
        """Each unmeasured factor and why, as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.unknown_json)

    @property
    def energy(self) -> dict[str, JsonValue]:
        """The MIK energy block as a fresh JSON object; the stored contract value remains immutable."""
        return _json.loads(self.energy_json)

    @property
    def rank_group(self) -> str:
        """Round 8: "measured" when alignment was measured, else "alignment unknown" (ranked below every measured
        one); "refused" when the candidate could not be rendered (ranked last)."""
        if self.refused is not None:
            return RANK_REFUSED
        return RANK_ALIGNMENT_UNKNOWN if "alignment" in self.unknown else RANK_MEASURED

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "energy": self.energy,
            "plan": self.plan,
            "plan_sha256": self.plan_sha256,
            "style": self.style,
            "bars": self.bars,
            "metrics": self.metrics,
            "factors": self.factors,
            "vetoes": [v.to_json() for v in self.vetoes],
            "measured": self.measured.to_json(),
            "unknown": self.unknown,
            "score": self.score,
            "scorer_version": self.scorer_version,
            "render_ms": self.render_ms,
            "score_ms": self.score_ms,
            "refused": self.refused,
            "render_path": self.render_path,
            "planner_confidence": self.planner_confidence,
            "planner_weakest_factor": self.planner_weakest_factor,
            "planner_confidence_title": self.planner_confidence_title,
            "rank_group": self.rank_group,
        }


@dataclass(frozen=True)
class TryResult:
    """Every candidate for one pair, best first (unscored ones last)."""

    out_id: str
    in_id: str
    candidates: tuple[Candidate, ...]
    scorer_version: str = SCORER_VERSION
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "out_id": self.out_id,
            "in_id": self.in_id,
            "scorer_version": self.scorer_version,
            "candidates": [c.to_json() for c in self.candidates],
            "notes": list(self.notes),
        }


RANK_MEASURED = "measured"
RANK_ALIGNMENT_UNKNOWN = "alignment unknown"
RANK_REFUSED = "refused"
_GROUP_ORDER = {RANK_MEASURED: 0, RANK_ALIGNMENT_UNKNOWN: 1, RANK_REFUSED: 2}


def rank_key(candidate: Candidate) -> tuple[int, int, float]:
    """Sort key, best first (round 8): every candidate whose alignment was measured ranks above every
    candidate whose alignment is unknown; within each group, scored by M, then unscored ones.

    A high M on a candidate nobody could check for beat alignment is not evidence it sounds aligned,
    so it never outranks one that was checked.
    """
    group = _GROUP_ORDER[candidate.rank_group]
    if candidate.score is None:
        return (group, 1, 0.0)
    return (group, 0, -candidate.score)


def best_candidate(candidates: tuple[Candidate, ...] | list[Candidate]) -> tuple[Candidate | None, str]:
    """ "Use best" (round 8): the highest-M candidate whose alignment was measured, scored and not vetoed.

    Returns (candidate, "") or (None, the reason there is none). Never falls back to a candidate whose
    alignment is unknown, unscored or vetoed: the caller shows the reason instead.
    """
    measured = [c for c in candidates if c.rank_group == RANK_MEASURED]
    eligible = [c for c in measured if c.score is not None and not c.vetoes]
    if eligible:
        return max(eligible, key=lambda c: c.score or 0.0), ""
    if not measured:
        return None, (
            f"no best: alignment is unknown on all {len(candidates)} candidates (too few beats could be timed), "
            "so none was checked for beat alignment"
        )
    return None, f"no best: the {len(measured)} candidate(s) with measured alignment are all vetoed or unscored"
