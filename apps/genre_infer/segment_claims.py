"""Segment-level enrichment claims on own structure windows (ENRICH-13622, parent #2491).

Vendor mood, energy, movement, voice, and instrument evidence is stored in the
claim envelope with ``scope.kind == segment`` bounded by Open DJ phrase or key
structure, not vendor segment edges. Track-level tags are aggregates of segment
claims, never a parallel whole-track compatibility score.

-Claude
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from apps.genre_infer.claim_envelope import (
    ClaimScope,
    EnrichmentClaim,
    SegmentScope,
    vendor_observation,
)
from apps.genre_infer import providers

SEGMENT_EVIDENCE_DIMENSIONS: frozenset[str] = frozenset({"mood", "energy", "movement", "vocals", "instrumentation"})
EVIDENCE_PTR_SCHEMA = "vendor-segment-evidence/v1"


@dataclass(frozen=True)
class OwnStructureWindow:
    """One published segment edge from own beatgrid or structure."""

    start_ms: int
    end_ms: int
    start_bar: int | None = None
    end_bar: int | None = None
    source: str = "own-structure"

    def validate(self) -> None:
        if self.start_ms < 0 or self.end_ms < 0:
            raise ValueError("structure window bounds must be non-negative")
        if self.end_ms < self.start_ms:
            raise ValueError("structure window end_ms must be >= start_ms")


@dataclass(frozen=True)
class VendorSegmentObservation:
    """One vendor-timed observation before alignment to own windows."""

    dimension: str
    value: Any
    vendor_start_ms: int
    vendor_end_ms: int
    confidence: float | None = None
    vendor: str = "cyanite"
    provider_model: str = "auto-tagging-2"
    schema_version: str = providers.CYANITE_EXPLORER_SCHEMA
    raw_blob_ptr: str = "blob://cyanite/segment/raw"

    def validate(self) -> None:
        dim = self.dimension.casefold()
        if dim not in SEGMENT_EVIDENCE_DIMENSIONS:
            raise ValueError(f"unsupported segment dimension {self.dimension!r}")
        if self.vendor_end_ms < self.vendor_start_ms:
            raise ValueError("vendor_end_ms must be >= vendor_start_ms")


@dataclass(frozen=True)
class GlobalMetadataSafety:
    """Whether track-level BPM/key may be published from own analysis."""

    safe: bool
    reason: str | None
    bpm_segments: tuple[float, ...] = ()
    key_segments: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "reason": self.reason,
            "bpm_segments": list(self.bpm_segments),
            "key_segments": list(self.key_segments),
        }


@dataclass(frozen=True)
class SegmentOverlayInput:
    """Phrase-local voice/instrument hints for loop and autoplay overlays."""

    start_ms: int
    end_ms: int
    voice_value: Any | None
    instrument_value: Any | None
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "voice_value": self.voice_value,
            "instrument_value": self.instrument_value,
            "reasons": list(self.reasons),
        }


def _ms_from_s(seconds: float) -> int:
    return int(round(float(seconds) * 1000.0))


def phrase_windows_from_sections(sections: Sequence[Mapping[str, Any]]) -> list[OwnStructureWindow]:
    """Own structure phrase windows in milliseconds."""
    out: list[OwnStructureWindow] = []
    for section in sections:
        start_s = section.get("start_s")
        end_s = section.get("end_s")
        if start_s is None or end_s is None:
            continue
        window = OwnStructureWindow(
            start_ms=_ms_from_s(float(start_s)),
            end_ms=_ms_from_s(float(end_s)),
            start_bar=section.get("start_bar"),
            end_bar=section.get("end_bar"),
            source=str(section.get("source") or "own-structure"),
        )
        window.validate()
        out.append(window)
    return out


def phrase_windows_from_payload(payload: Mapping[str, Any]) -> list[OwnStructureWindow]:
    """Phrase windows from an ``/anlz`` payload's ``phrases`` list."""
    phrases = payload.get("phrases")
    if not isinstance(phrases, Sequence):
        return []
    sections: list[dict[str, Any]] = []
    for phrase in phrases:
        if not isinstance(phrase, Mapping):
            continue
        sections.append(dict(phrase))
    return phrase_windows_from_sections(sections)


def key_segment_windows(
    segments_block: Mapping[str, Any], *, duration_s: float | None = None
) -> list[OwnStructureWindow]:
    """Own key-segment windows; empty when the block is not ``ok``."""
    if segments_block.get("status") != "ok":
        return []
    segments = segments_block.get("segments")
    if not isinstance(segments, Sequence):
        return []
    out: list[OwnStructureWindow] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        start_s = segment.get("start_s")
        end_s = segment.get("end_s")
        if start_s is None:
            continue
        if end_s is None and duration_s is not None:
            end_s = duration_s
        if end_s is None:
            continue
        out.append(
            OwnStructureWindow(
                start_ms=_ms_from_s(float(start_s)),
                end_ms=_ms_from_s(float(end_s)),
                start_bar=segment.get("start_bar"),
                end_bar=segment.get("end_bar"),
                source="own-key-segment",
            )
        )
    return out


def _overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def align_vendor_to_own(
    observation: VendorSegmentObservation, windows: Sequence[OwnStructureWindow]
) -> OwnStructureWindow | None:
    """Pick the own window with the greatest overlap; published edges stay own."""
    best: OwnStructureWindow | None = None
    best_overlap = -1
    for window in windows:
        overlap = _overlap_ms(
            window.start_ms,
            window.end_ms,
            observation.vendor_start_ms,
            observation.vendor_end_ms,
        )
        if overlap > best_overlap:
            best_overlap = overlap
            best = window
    return best


def vendor_evidence_pointer(observation: VendorSegmentObservation, own: OwnStructureWindow) -> str:
    """Serialize vendor times as evidence; scope uses ``own`` bounds only."""
    payload = {
        "schema": EVIDENCE_PTR_SCHEMA,
        "vendor": observation.vendor,
        "vendor_start_ms": observation.vendor_start_ms,
        "vendor_end_ms": observation.vendor_end_ms,
        "own_start_ms": own.start_ms,
        "own_end_ms": own.end_ms,
        "raw_blob_ptr": observation.raw_blob_ptr,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_segment_claim(
    *,
    stable_id: str,
    observation: VendorSegmentObservation,
    own: OwnStructureWindow,
    input_fingerprint: str,
) -> EnrichmentClaim:
    """One semantic segment claim on an own structure window."""
    observation.validate()
    own.validate()
    scope = ClaimScope(kind="segment", segment=SegmentScope(own.start_ms, own.end_ms))
    return vendor_observation(
        stable_id=stable_id,
        dimension=observation.dimension,
        value=observation.value,
        source=observation.vendor,
        provider_model=observation.provider_model,
        schema_version=observation.schema_version,
        input_fingerprint=input_fingerprint,
        raw_evidence_ptr=vendor_evidence_pointer(observation, own),
        confidence=observation.confidence,
        scope=scope,
    )


def attach_vendor_observations(
    *,
    stable_id: str,
    windows: Sequence[OwnStructureWindow],
    observations: Sequence[VendorSegmentObservation],
    input_fingerprint: str,
) -> list[EnrichmentClaim]:
    """Attach vendor observations to own windows; skip unmatched spans."""
    claims: list[EnrichmentClaim] = []
    for observation in observations:
        own = align_vendor_to_own(observation, windows)
        if (
            own is None
            or _overlap_ms(
                own.start_ms,
                own.end_ms,
                observation.vendor_start_ms,
                observation.vendor_end_ms,
            )
            <= 0
        ):
            continue
        claims.append(
            build_segment_claim(
                stable_id=stable_id,
                observation=observation,
                own=own,
                input_fingerprint=input_fingerprint,
            )
        )
    return claims


def aggregate_track_summary(claims: Sequence[EnrichmentClaim]) -> dict[str, Any]:
    """Track-level summary derived from segment claims only (not a similarity score)."""
    by_dimension: dict[str, list[Any]] = {dim: [] for dim in SEGMENT_EVIDENCE_DIMENSIONS}
    for claim in claims:
        if claim.scope.kind != "segment" or claim.scope.segment is None:
            continue
        dim = claim.dimension.casefold()
        if dim in by_dimension:
            by_dimension[dim].append(claim.value)
    summary: dict[str, Any] = {"kind": "segment_aggregate", "dimensions": {}}
    for dim, values in by_dimension.items():
        if not values:
            continue
        if all(isinstance(v, (int, float)) for v in values):
            summary["dimensions"][dim] = {
                "mean": round(sum(float(v) for v in values) / len(values), 4),
                "segments": len(values),
            }
        else:
            summary["dimensions"][dim] = {"mode": _mode(values), "segments": len(values)}
    return summary


def _mode(values: Sequence[Any]) -> Any:
    counts: dict[str, int] = {}
    for value in values:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        counts[key] = counts.get(key, 0) + 1
    winner = max(counts.items(), key=lambda item: item[1])[0]
    return json.loads(winner)


def global_metadata_safety(
    *,
    track_bpm: float | None,
    track_key: str | None,
    segment_bpms: Sequence[float],
    segment_keys: Sequence[str],
    bpm_tolerance_pct: float = 3.0,
) -> GlobalMetadataSafety:
    """Mark global BPM/key unsafe when own segments disagree materially."""
    bpm_vals = tuple(segment_bpms)
    key_vals = tuple(segment_keys)
    if len(bpm_vals) >= 2:
        spread = (max(bpm_vals) - min(bpm_vals)) / max(min(bpm_vals), 1e-6) * 100.0
        if spread > bpm_tolerance_pct:
            return GlobalMetadataSafety(
                safe=False,
                reason="segment_bpm_disagreement",
                bpm_segments=bpm_vals,
                key_segments=key_vals,
            )
    if len(key_vals) >= 2 and len(set(key_vals)) > 1:
        return GlobalMetadataSafety(
            safe=False,
            reason="segment_key_disagreement",
            bpm_segments=bpm_vals,
            key_segments=key_vals,
        )
    if (
        track_bpm is not None
        and bpm_vals
        and any(abs(v - track_bpm) / max(track_bpm, 1e-6) * 100.0 > bpm_tolerance_pct for v in bpm_vals)
    ):
        return GlobalMetadataSafety(
            safe=False,
            reason="vendor_summary_overwrites_own_bpm",
            bpm_segments=bpm_vals,
            key_segments=key_vals,
        )
    if track_key is not None and key_vals and any(k != track_key for k in key_vals):
        return GlobalMetadataSafety(
            safe=False,
            reason="vendor_summary_overwrites_own_key",
            bpm_segments=bpm_vals,
            key_segments=key_vals,
        )
    return GlobalMetadataSafety(safe=True, reason=None, bpm_segments=bpm_vals, key_segments=key_vals)


def m_with_semantic_overlay(
    *,
    score: float | None,
    vetoes: Sequence[Any],
    semantic_factor: float,
    max_lift: float = 0.15,
) -> float | None:
    """Semantic segment evidence never raises M above zero when layer-0 vetoed."""
    if vetoes:
        return 0.0
    if score is None:
        return None
    lift = max(0.0, min(max_lift, semantic_factor * max_lift))
    return round(min(1.0, score + lift), 4)


def semantic_alignment_factor(claims: Sequence[EnrichmentClaim], overlap_ms: tuple[int, int]) -> float:
    """A bounded [0,1] hint from segment claims overlapping a transition window."""
    start_ms, end_ms = overlap_ms
    matched = 0
    for claim in claims:
        if claim.scope.kind != "segment" or claim.scope.segment is None:
            continue
        seg = claim.scope.segment
        if _overlap_ms(seg.start_ms, seg.end_ms, start_ms, end_ms) <= 0:
            continue
        matched += 1
    if matched == 0:
        return 0.0
    return min(1.0, math.log1p(matched) / math.log1p(5))


def overlay_inputs_for_phrase(claims: Sequence[EnrichmentClaim], window: OwnStructureWindow) -> SegmentOverlayInput:
    """Voice/instrument segment claims as overlay inputs, not a compatibility number."""
    voice: Any | None = None
    instrument: Any | None = None
    reasons: list[str] = []
    for claim in claims:
        if claim.scope.kind != "segment" or claim.scope.segment is None:
            continue
        seg = claim.scope.segment
        if _overlap_ms(seg.start_ms, seg.end_ms, window.start_ms, window.end_ms) <= 0:
            continue
        dim = claim.dimension.casefold()
        if dim == "vocals":
            voice = claim.value
            reasons.append(f"voice segment {claim.value!r} on own window")
        elif dim == "instrumentation":
            instrument = claim.value
            reasons.append(f"instrument segment {claim.value!r} on own window")
    if voice is None and instrument is None:
        reasons.append("no voice/instrument segment claims on phrase window")
    return SegmentOverlayInput(
        start_ms=window.start_ms,
        end_ms=window.end_ms,
        voice_value=voice,
        instrument_value=instrument,
        reasons=tuple(reasons),
    )


def claims_covering_dimensions(claims: Sequence[EnrichmentClaim]) -> set[str]:
    """Dimensions present as segment-scoped claims."""
    found: set[str] = set()
    for claim in claims:
        if claim.scope.kind == "segment" and claim.scope.segment is not None:
            found.add(claim.dimension.casefold())
    return found


__all__ = [
    "EVIDENCE_PTR_SCHEMA",
    "GlobalMetadataSafety",
    "OwnStructureWindow",
    "SEGMENT_EVIDENCE_DIMENSIONS",
    "SegmentOverlayInput",
    "VendorSegmentObservation",
    "aggregate_track_summary",
    "align_vendor_to_own",
    "attach_vendor_observations",
    "build_segment_claim",
    "claims_covering_dimensions",
    "global_metadata_safety",
    "key_segment_windows",
    "m_with_semantic_overlay",
    "overlay_inputs_for_phrase",
    "phrase_windows_from_payload",
    "phrase_windows_from_sections",
    "semantic_alignment_factor",
    "vendor_evidence_pointer",
]
