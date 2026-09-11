"""Shared vocabulary for :mod:`apps.mik.load` and :mod:`apps.mik.load_plan`:
the load-policy constants and the plan's dataclasses.

Split into its own module purely to break the import cycle a plan-building
module would otherwise have with ``load.py`` (``load_plan`` needs these;
``load.py`` needs ``load_plan``'s ``build_plan`` back). ``load.py``
re-exports every name here, so ``from apps.mik.load import LoadPlan`` (the
existing import shape used throughout the codebase) is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mikdb import EnergySegment

# MIK's flat analysis confidence, per CONTEXT D4 / apps.tags.unify
# SOURCE_CONFIDENCE. Key does NOT use this: it uses MIK's own per-track
# ZKEYSEGMENT.ZCONFIDENCE.
MIK_CONFIDENCE = 0.95

# Key precedence is a THREE-band policy, not a single floor. Superseded the
# earlier flat 0.70 gate on Tue 28 Jul 2026: the only peer-reviewed benchmark of
# commercial key detection (Knees et al., ISMIR 2015, GiantSteps Key n=604,
# MIREX weighted) ranks rekordbox 79.55 ABOVE MIK 74.60, so MIK does not get to
# win on reputation. Observed bands across MIK's 7,019 key segments:
# >= 0.90 -> 4,272 tracks, 0.70-0.90 -> 1,419, < 0.70 -> 1,328.
KEY_CONFIDENCE_MIK_WINS = 0.90
KEY_CONFIDENCE_FLOOR = 0.70

SEGMENTS_FIELD = "energy_segments"

# Every field this loader can touch, and its gate key. The gate key is our
# field name, so unit B's verdict file and this table share a vocabulary.
SCALAR_FIELDS: tuple[str, ...] = (
    "key",
    "energy",
    "bpm",
    "loudness",
    "clipped_peak_count",
)
GATED_FIELDS: tuple[str, ...] = (*SCALAR_FIELDS, SEGMENTS_FIELD)

# Sources that outrank MIK per-field. MIK never outranks rekordbox on bpm;
# on key it does, but only above the confidence floor and only with
# --overwrite-lower-precedence.
OUTRANKS_MIK: dict[str, frozenset[str]] = {
    "bpm": frozenset({"rekordbox", "manual", "webui"}),
    "key": frozenset({"manual", "webui"}),
    "energy": frozenset({"manual", "webui"}),
    "loudness": frozenset({"manual", "webui"}),
    "clipped_peak_count": frozenset({"manual", "webui"}),
}


class LoadError(RuntimeError):
    """The load cannot proceed safely."""


@dataclass(frozen=True)
class FieldWrite:
    stable_id: str
    field_name: str
    value: Any
    confidence: float | None
    modified_at: str
    tier: str


@dataclass(frozen=True)
class SegmentWrite:
    stable_id: str
    segments: tuple[EnergySegment, ...]
    confidence: float | None
    modified_at: str
    tier: str


@dataclass(frozen=True)
class StagedWrite:
    source_row_id: str
    field_name: str
    value: Any
    confidence: float | None
    unmatched_reason: str
    title: str | None
    artist: str | None
    album: str | None
    duration_ms: int | None
    source_path: str | None
    modified_at: str


@dataclass
class LoadPlan:
    field_writes: list[FieldWrite] = field(default_factory=list)
    segment_writes: list[SegmentWrite] = field(default_factory=list)
    staged: list[StagedWrite] = field(default_factory=list)
    blocked_by_gate: dict[str, int] = field(default_factory=dict)
    blocked_by_precedence: dict[str, int] = field(default_factory=dict)
    blocked_by_promotion_conflict: dict[str, int] = field(default_factory=dict)
    blocked_by_key_floor: int = 0
    key_review_band: list[str] = field(default_factory=list)
    no_value: dict[str, int] = field(default_factory=dict)
    gate_summary: dict[str, str] = field(default_factory=dict)
    verification: list[dict[str, Any]] = field(default_factory=list)
    """Per-field verification provenance, captured at plan time and persisted
    by :func:`apply_plan` into ``analysis_field_verification``."""

    @property
    def segment_row_count(self) -> int:
        return sum(len(write.segments) for write in self.segment_writes)

    def _bump(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1
