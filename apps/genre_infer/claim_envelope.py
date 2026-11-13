"""Vendor-neutral enrichment claim envelope (ENRICH-12086, parent #2491).

Every semantic tag, generated description, or deterministic musical fact from
own analysis or a paid supplier is stored in this shape so provenance survives
import, re-analysis, and optional vendor churn. See
``(private path)``.

-Claude
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

ENVELOPE_VERSION = "enrichment-claim/v1"

ClaimKind = Literal["semantic_tag", "generated_description", "deterministic_fact"]
ScopeKind = Literal["track", "segment"]

DETERMINISTIC_DIMENSIONS: frozenset[str] = frozenset({"bpm", "key", "meter"})
SEMANTIC_DIMENSIONS: frozenset[str] = frozenset(
    {
        "genre",
        "mood",
        "movement",
        "instrumentation",
        "vocals",
        "lyric_theme",
        "tempo_perceived",
        "energy",
        "character",
    }
)
# Segment-scoped mood/energy/movement/voice/instrument evidence (ENRICH-13622).
SEGMENT_EVIDENCE_DIMENSIONS: frozenset[str] = frozenset({"mood", "energy", "movement", "vocals", "instrumentation"})
GENERATED_DIMENSIONS: frozenset[str] = frozenset({"description", "lyric_transcript"})


class ClaimEnvelopeError(ValueError):
    """A claim fails validation before storage."""


@dataclass(frozen=True)
class SegmentScope:
    start_ms: int
    end_ms: int

    def validate(self) -> None:
        if self.start_ms < 0 or self.end_ms < 0:
            raise ClaimEnvelopeError("segment bounds must be non-negative")
        if self.end_ms < self.start_ms:
            raise ClaimEnvelopeError("segment end_ms must be >= start_ms")


@dataclass(frozen=True)
class ClaimScope:
    """Whole track or one time segment (segment timestamps in milliseconds)."""

    kind: ScopeKind
    segment: SegmentScope | None = None

    def validate(self) -> None:
        if self.kind == "track" and self.segment is not None:
            raise ClaimEnvelopeError("track scope must not carry segment bounds")
        if self.kind == "segment":
            if self.segment is None:
                raise ClaimEnvelopeError("segment scope needs start_ms and end_ms")
            self.segment.validate()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.segment is not None:
            out["segment"] = asdict(self.segment)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ClaimScope:
        seg = raw.get("segment")
        segment = None if seg is None else SegmentScope(int(seg["start_ms"]), int(seg["end_ms"]))
        return cls(kind=raw["kind"], segment=segment)


@dataclass(frozen=True)
class EnrichmentClaim:
    """One versioned enrichment observation bound to a track (or segment)."""

    stable_id: str
    dimension: str
    kind: ClaimKind
    source: str
    provider_model: str
    schema_version: str
    scope: ClaimScope
    value: Any
    confidence: float | None
    generated_at: str
    input_fingerprint: str
    raw_evidence_ptr: str
    envelope_version: str = ENVELOPE_VERSION
    claim_id: str | None = None

    def validate(self) -> None:
        if not self.stable_id.strip():
            raise ClaimEnvelopeError("stable_id is required")
        if not self.dimension.strip():
            raise ClaimEnvelopeError("dimension is required")
        if not self.source.strip():
            raise ClaimEnvelopeError("source is required")
        if not self.provider_model.strip():
            raise ClaimEnvelopeError("provider_model is required")
        if not self.schema_version.strip():
            raise ClaimEnvelopeError("schema_version is required")
        if not self.input_fingerprint.strip():
            raise ClaimEnvelopeError("input_fingerprint is required")
        if not self.raw_evidence_ptr.strip():
            raise ClaimEnvelopeError("raw_evidence_ptr is required")
        if self.envelope_version != ENVELOPE_VERSION:
            raise ClaimEnvelopeError(f"unsupported envelope_version {self.envelope_version!r}")
        self.scope.validate()
        _validate_kind_dimension(self.kind, self.dimension)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ClaimEnvelopeError("confidence must be in [0, 1] when set")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "claim_id": self.claim_id,
            "confidence": self.confidence,
            "dimension": self.dimension,
            "envelope_version": self.envelope_version,
            "generated_at": self.generated_at,
            "input_fingerprint": self.input_fingerprint,
            "kind": self.kind,
            "provider_model": self.provider_model,
            "raw_evidence_ptr": self.raw_evidence_ptr,
            "schema_version": self.schema_version,
            "scope": self.scope.as_dict(),
            "source": self.source,
            "stable_id": self.stable_id,
            "value": self.value,
        }
        return {k: v for k, v in payload.items() if v is not None}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EnrichmentClaim:
        claim = cls(
            stable_id=raw["stable_id"],
            dimension=raw["dimension"],
            kind=raw["kind"],
            source=raw["source"],
            provider_model=raw["provider_model"],
            schema_version=raw["schema_version"],
            scope=ClaimScope.from_dict(raw["scope"]),
            value=raw["value"],
            confidence=raw.get("confidence"),
            generated_at=raw["generated_at"],
            input_fingerprint=raw["input_fingerprint"],
            raw_evidence_ptr=raw["raw_evidence_ptr"],
            envelope_version=raw.get("envelope_version", ENVELOPE_VERSION),
            claim_id=raw.get("claim_id"),
        )
        claim.validate()
        return claim

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> EnrichmentClaim:
        return cls.from_dict(json.loads(text))


def _validate_kind_dimension(kind: ClaimKind, dimension: str) -> None:
    dim = dimension.casefold()
    if kind == "deterministic_fact" and dim not in DETERMINISTIC_DIMENSIONS:
        raise ClaimEnvelopeError(f"deterministic_fact must use {sorted(DETERMINISTIC_DIMENSIONS)}; got {dimension!r}")
    if kind == "generated_description" and dim not in GENERATED_DIMENSIONS:
        raise ClaimEnvelopeError(f"generated_description must use {sorted(GENERATED_DIMENSIONS)}; got {dimension!r}")
    if kind == "semantic_tag" and dim in DETERMINISTIC_DIMENSIONS:
        raise ClaimEnvelopeError(f"semantic_tag must not use deterministic dimension {dimension!r}")


def deterministic_fact(
    *,
    stable_id: str,
    dimension: Literal["bpm", "key", "meter"],
    value: Any,
    source: str,
    provider_model: str,
    schema_version: str,
    input_fingerprint: str,
    raw_evidence_ptr: str,
    confidence: float | None = None,
    generated_at: str | None = None,
    scope: ClaimScope | None = None,
) -> EnrichmentClaim:
    """Build a claim for Open DJ's own BPM/key/meter analysis (never semantic prose)."""
    claim = EnrichmentClaim(
        stable_id=stable_id,
        dimension=dimension,
        kind="deterministic_fact",
        source=source,
        provider_model=provider_model,
        schema_version=schema_version,
        scope=scope or ClaimScope(kind="track"),
        value=value,
        confidence=confidence,
        generated_at=generated_at or _now(),
        input_fingerprint=input_fingerprint,
        raw_evidence_ptr=raw_evidence_ptr,
    )
    claim.validate()
    return claim


def vendor_observation(
    *,
    stable_id: str,
    dimension: str,
    value: Any,
    source: str,
    provider_model: str,
    schema_version: str,
    input_fingerprint: str,
    raw_evidence_ptr: str,
    confidence: float | None = None,
    generated_at: str | None = None,
    scope: ClaimScope | None = None,
    generated: bool = False,
) -> EnrichmentClaim:
    """Build a semantic tag or generated-description claim from a supplier."""
    kind: ClaimKind = "generated_description" if generated else "semantic_tag"
    claim = EnrichmentClaim(
        stable_id=stable_id,
        dimension=dimension,
        kind=kind,
        source=source,
        provider_model=provider_model,
        schema_version=schema_version,
        scope=scope or ClaimScope(kind="track"),
        value=value,
        confidence=confidence,
        generated_at=generated_at or _now(),
        input_fingerprint=input_fingerprint,
        raw_evidence_ptr=raw_evidence_ptr,
    )
    claim.validate()
    return claim


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "ClaimEnvelopeError",
    "ClaimKind",
    "ClaimScope",
    "DETERMINISTIC_DIMENSIONS",
    "ENVELOPE_VERSION",
    "EnrichmentClaim",
    "GENERATED_DIMENSIONS",
    "SEGMENT_EVIDENCE_DIMENSIONS",
    "SEMANTIC_DIMENSIONS",
    "SegmentScope",
    "deterministic_fact",
    "vendor_observation",
]
