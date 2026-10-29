"""The try-er contract: what the execution layer hands the endpoint (TRYER-03).

``try_pair`` proposes N candidate transitions for one pair, renders each
offline through ``odj-audio render --plan --decks-out``, scores each from the
rendered audio and returns them ranked. The endpoint (``/api/v1/dj/try``,
``opendj dj try``, MCP ``try_transitions``) imports these types and nothing
else from this package.

A candidate's ``score`` is the macro measure M (file 15): ``0.0`` when a veto
fired (named in ``vetoes``), ``None`` when fewer than
:data:`MIN_CORE_MEASURED` of the core factors could be measured, otherwise the
weighted geometric mean of the measured layer-1 factors. ``unknown`` names
every factor that was not measured and why; it is never read as a pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: Pinned per round (spec ``transitions-tryer.md`` section 5). 0.2.0 = round 1 (kick-only template, dominant-peak gate);
#: 0.2.1 = round 2 (cut timing held to the downbeat as rendered);
#: 0.3.0 = round 6 (outgoing kick must dominate its own beat; alignment per overlap quarter);
#: 0.4.0 = round 7 (coverage rule: 16 timed beats and 2 in at least 3 of 4 quarters);
#: 0.5.0 = round 10 (a beat whose kick band is absent is timed on the 2-8 kHz band);
#: 0.6.0 = round 14 (loudness_continuity weight 0.4; loudness_loss veto, wind-down exempt);
#: 0.7.0 = round 15 (bass_overlap counts beats with both lows near full; low_continuity factor; true peak vs the louder deck).
#: Versioning rule: ADR-NEW-transition-quality-measure.
SCORER_VERSION = "0.7.0"
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

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Measured:
    """How many of the core factors were measured (k of n)."""

    k: int
    n: int

    def to_json(self) -> dict[str, int]:
        return {"k": self.k, "n": self.n}


@dataclass(frozen=True)
class ScoreResult:
    """The scorer's verdict on one render."""

    metrics: dict[str, Any]
    factors: dict[str, float]
    vetoes: tuple[Veto, ...]
    measured: Measured
    unknown: dict[str, str]
    score: float | None
    scorer_version: str = SCORER_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "factors": self.factors,
            "vetoes": [v.to_json() for v in self.vetoes],
            "measured": self.measured.to_json(),
            "unknown": self.unknown,
            "score": self.score,
            "scorer_version": self.scorer_version,
        }


@dataclass(frozen=True)
class Candidate:
    """One rendered and scored transition plan."""

    id: str
    plan: dict[str, Any]
    plan_sha256: str
    style: str
    bars: int
    metrics: dict[str, Any]
    factors: dict[str, float]
    vetoes: tuple[Veto, ...]
    measured: Measured
    unknown: dict[str, str]
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

    @property
    def rank_group(self) -> str:
        """Round 8: "measured" when alignment was measured, else "alignment unknown" (ranked below every measured
        one); "refused" when the candidate could not be rendered (ranked last)."""
        if self.refused is not None:
            return RANK_REFUSED
        return RANK_ALIGNMENT_UNKNOWN if "alignment" in self.unknown else RANK_MEASURED

    def to_json(self) -> dict[str, Any]:
        document = asdict(self)
        document["vetoes"] = [v.to_json() for v in self.vetoes]
        document["measured"] = self.measured.to_json()
        document["rank_group"] = self.rank_group
        return document


@dataclass(frozen=True)
class TryResult:
    """Every candidate for one pair, best first (unscored ones last)."""

    out_id: str
    in_id: str
    candidates: tuple[Candidate, ...]
    scorer_version: str = SCORER_VERSION
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
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
