"""Per-field precedence + provenance for tag unification (CONTEXT D4).

Input: a :class:`TagSources` bundle (one TagRead per source, None if the
source had nothing).
Output: :class:`UnifiedTags` + a per-field provenance log.

The precedence policy is data-driven so changing it is a one-line edit:
swap ``DEFAULT_POLICY`` for a custom ``PrecedencePolicy``. Confidence
scoring per source follows the CONTEXT D4 table:

* MIK:        0.95 (authoritative analysis)
* RB user:    0.90 (canonical user-edit source)
* ID3/file:   0.70
* djay:       0.65
* filename:   0.30 (last resort)

Precedence is ``DEFAULT_POLICY`` source order; ``SOURCE_CONFIDENCE`` is
stamped on the winner afterwards and does not reorder the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apps.shared.tag_writer import TagRead, UnifiedTags

SOURCES = ("rb", "mik", "djay", "file", "filename")


@dataclass(frozen=True)
class TagSources:
    rb: TagRead | None = None
    mik: TagRead | None = None
    djay: TagRead | None = None
    file: TagRead | None = None
    filename: TagRead | None = None


@dataclass(frozen=True)
class Provenance:
    source: str
    confidence: float
    modified_at: str  # ISO-8601 UTC
    attempts: tuple[tuple[str, bool], ...] = ()  # (source, had_value) log


@dataclass(frozen=True)
class UnifiedPlan:
    tags: UnifiedTags
    provenance: dict[str, Provenance]


SOURCE_CONFIDENCE: dict[str, float] = {
    "mik": 0.95,
    "rb": 0.90,
    "file": 0.70,
    "djay": 0.65,
    "filename": 0.30,
}


@dataclass(frozen=True)
class PrecedencePolicy:
    """Ordered source list per field."""

    by_field: dict[str, tuple[str, ...]]


# Per CONTEXT D4. For MIK-ish fields (key, bpm, energy), MIK is first; for
# user-typed fields (title/artist/album), RB is first.
DEFAULT_POLICY = PrecedencePolicy(
    by_field={
        "title": ("rb", "file", "filename", "djay"),
        "artist": ("rb", "file", "filename", "djay"),
        "album": ("rb", "file", "djay"),
        "genre": ("rb", "mik", "file", "djay"),
        "key_openkey": ("mik", "rb", "djay", "file"),
        "key_camelot": ("mik", "rb", "djay", "file"),
        "bpm": ("mik", "rb", "djay", "file"),
        "energy": ("mik", "rb", "file"),
        "rating": ("rb", "djay"),
        "isrc": ("file", "rb", "djay"),
    }
)


# --------------------------------------------------------------- sanity


def _valid_bpm(v: float | None) -> bool:
    return v is not None and 40.0 <= v <= 220.0


def _valid_energy(v: int | None) -> bool:
    return v is not None and 1 <= v <= 10


def _valid_key(v: str | None) -> bool:
    return bool(v and v.strip())


SANITY: dict[str, Any] = {
    "bpm": _valid_bpm,
    "energy": _valid_energy,
    "key_openkey": _valid_key,
    "key_camelot": _valid_key,
}


def _has_value(tr: TagRead | None, field_name: str) -> bool:
    if tr is None:
        return False
    val = getattr(tr, field_name, None)
    if val is None:
        return False
    # String fields: empty-string counts as missing.
    if isinstance(val, str) and not val.strip():
        return False
    sanity_fn = SANITY.get(field_name)
    if sanity_fn is not None and not sanity_fn(val):
        return False
    return True


def _get_value(tr: TagRead, field_name: str) -> Any:
    return getattr(tr, field_name)


def _source_lookup(sources: TagSources) -> dict[str, TagRead | None]:
    """Map public source names to the corresponding ``TagSources`` slot."""
    return {name: getattr(sources, name) for name in SOURCES}


def _resolve_field(
    field_name: str,
    chain: tuple[str, ...],
    src_lookup: dict[str, TagRead | None],
    *,
    now_iso: str,
) -> tuple[Any | None, Provenance | None]:
    """Walk ``chain``; return the first valid value and its provenance, if any."""
    attempts: list[tuple[str, bool]] = []
    winner_source: str | None = None
    for src in chain:
        tr = src_lookup.get(src)
        ok = _has_value(tr, field_name)
        attempts.append((src, ok))
        if ok and winner_source is None:
            winner_source = src
    if winner_source is None:
        return None, None
    winner_tr = src_lookup[winner_source]
    if winner_tr is None:
        return None, None
    value = _get_value(winner_tr, field_name)
    provenance = Provenance(
        source=winner_source,
        confidence=SOURCE_CONFIDENCE[winner_source],
        modified_at=now_iso,
        attempts=tuple(attempts),
    )
    return value, provenance


# -------------------------------------------------------------- main op


def unify(
    sources: TagSources,
    *,
    policy: PrecedencePolicy = DEFAULT_POLICY,
    now: datetime | None = None,
) -> UnifiedPlan:
    """Walk each field's precedence list; pick first valid; record log.

    Deterministic: same input -> same output (including attempts log).
    """
    now_iso = (now or datetime.now(UTC)).isoformat()
    src_lookup = _source_lookup(sources)

    chosen: dict[str, Any] = {}
    provenance: dict[str, Provenance] = {}

    for field_name, chain in policy.by_field.items():
        value, field_provenance = _resolve_field(field_name, chain, src_lookup, now_iso=now_iso)
        if field_provenance is None:
            continue
        chosen[field_name] = value
        provenance[field_name] = field_provenance

    return UnifiedPlan(
        tags=UnifiedTags(**chosen),
        provenance=provenance,
    )


__all__ = [
    "DEFAULT_POLICY",
    "PrecedencePolicy",
    "Provenance",
    "SOURCE_CONFIDENCE",
    "SOURCES",
    "TagSources",
    "UnifiedPlan",
    "unify",
]
