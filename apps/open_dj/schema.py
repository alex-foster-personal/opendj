"""Typed dataclass layer for open-dj v0.2 libraries (Phase 15).

These dataclasses are the canonical Python representation of an open-dj
library in-memory. They are a pragmatic subset of the v0.2 JSON Schema
(``open-dj/schema/v0.2/open-dj.schema.json``) shaped for Phase 16 adapter
round-trip use. The JSON Schema is the source of truth for on-disk
documents; these types are the source of truth for adapter APIs.

Design notes:

* ``Track``/``Playlist``/``OpenDjLibrary`` are frozen for safety: adapters
  construct and hand off; callers pass through ``canon.to_canonical_bytes``.
* ``CuePoint`` uses a flat ``type`` literal covering the core enum plus
  ``loop`` as a convenience alias (adapters map to ``loop_in``/``loop_out``
  pairs when writing to the schema).
* ``BeatGridPoint`` captures one beat anchor (position + locked BPM). The
  JSON Schema models beats as a single ``BeatGrid`` object with a
  ``beats`` array; the typed layer exposes per-anchor points so adapters
  can round-trip variable-tempo grids without collapsing them.
* ``AdapterReport`` is mutable by design: adapters ``bump`` counters and
  ``warn`` on lossy translations as they stream through a library.
* ``Adapter`` is a ``runtime_checkable`` Protocol; third-party adapters
  just need to implement ``name``, ``read``, ``write``, ``capabilities``.

See also:

* :mod:`apps.open_dj.canon`       -- RFC 8785 JCS canonical bytes.
* :mod:`apps.open_dj.id`          -- spec section 5 identity chain.
* :mod:`apps.open_dj.validate`    -- JSON Schema validator.
* :mod:`apps.open_dj.provenance`  -- ProvenanceValue envelope helpers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

# ----------------------------------------------------------- track types


CueType = Literal[
    "hot",
    "memory",
    "loop",
    "load",
    "fade_in",
    "fade_out",
    "grid",
]
"""Open-dj cue-point type literal, as consumed by Phase 16 adapters.

The JSON Schema enum additionally splits ``loop`` into ``loop_in``/
``loop_out``; adapters translate that at the wire edge.
"""


VendorId = Literal[
    "mik",
    "rekordbox",
    "djay",
    "serato",
    "traktor",
    "open-dj-tool",
    "manual",
    "inferred",
]
"""ProvenanceValue.source enum -- every known open-dj ingest origin."""


SupportLevel = Literal["lossless", "lossy", "unsupported"]
"""Capability-table support level, one per open-dj field per adapter."""


WarningAction = Literal[
    "dropped",
    "downgraded",
    "stored_as_extension",
    "synthesised",
]
"""Structured-warning action verb -- what the adapter did on a lossy path."""


@dataclass(frozen=True)
class BeatGridPoint:
    """One beat anchor: a position (ms) + the BPM locked from that point.

    ``terminal`` marks the last anchor in a variable-tempo grid so writers
    can emit the end-of-grid sentinel some vendors require (e.g. Serato).
    """

    position_ms: int
    bpm: float
    terminal: bool = False


@dataclass(frozen=True)
class CuePoint:
    """A hot cue, memory cue, loop, or flip marker.

    ``type`` matches the open-dj enum: ``hot``, ``memory``, ``loop``,
    ``load``, ``fade_in``, ``fade_out``, ``grid``.
    """

    index: int
    position_ms: int
    type: CueType
    name: str = ""
    color_rgb: int | None = None
    length_ms: int | None = None


@dataclass(frozen=True)
class Track:
    """Open-dj v0.2 Track (typed layer consumed by Phase 16 adapters).

    Fields map to the JSON Schema Track object with these conveniences:

    * Scalar ``bpm``/``key_camelot``/``rating`` rather than
      ``ProvenanceValue`` envelopes. Adapters wrap with provenance at the
      JCS edge via :mod:`apps.open_dj.provenance`.
    * ``artists`` is an ordered tuple of strings.
    * ``cues``/``beats`` are tuples of the per-anchor dataclasses above.
    * ``extensions`` holds ``x_*`` passthrough data.
    """

    track_id: str
    file_path: str
    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    bpm: float | None = None
    key_camelot: str | None = None
    rating: int | None = None          # 0-5; None == unset
    duration_ms: int | None = None
    play_count: int = 0
    color_rgb: int | None = None
    cues: tuple[CuePoint, ...] = ()
    beats: tuple[BeatGridPoint, ...] = ()
    isrc: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Playlist:
    """Ordered playlist, with nested folders expressed via ``children``."""

    name: str
    track_ids: tuple[str, ...]
    children: tuple["Playlist", ...] = ()


@dataclass(frozen=True)
class OpenDjLibrary:
    """Canonical in-memory open-dj library. Adapter ``read()`` returns this."""

    version: str
    tracks: tuple[Track, ...]
    playlists: tuple[Playlist, ...] = ()
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation (tuples kept as lists by asdict)."""
        return asdict(self)


# --------------------------------------------------------- capabilities


@dataclass(frozen=True)
class CapabilityField:
    """One row of the capability table (field + support + rationale)."""

    field: str
    supports: SupportLevel
    rationale: str = ""


@dataclass(frozen=True)
class Capabilities:
    """Per-adapter capability descriptor (open-dj section 6 + 7 lossy lanes)."""

    adapter: str
    version: str
    fields: tuple[CapabilityField, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "version": self.version,
            "fields": [asdict(f) for f in self.fields],
        }


# --------------------------------------------------------------- warnings


@dataclass(frozen=True)
class Warning:
    """Structured adapter warning (open-dj section 6 lossy-lane reporting)."""

    field: str
    track_id: str
    action: WarningAction
    reason: str


@dataclass
class AdapterReport:
    """Mutable report returned alongside adapter ``read()`` / ``write()``.

    Use :meth:`bump` to increment a named counter and :meth:`warn` to
    append a structured :class:`Warning`.
    """

    warnings: list[Warning] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, by: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + by

    def warn(
        self,
        *,
        field: str,
        track_id: str,
        action: WarningAction,
        reason: str,
    ) -> None:
        self.warnings.append(
            Warning(field=field, track_id=track_id, action=action, reason=reason)
        )


# ---------------------------------------------------------- Adapter API


@runtime_checkable
class Adapter(Protocol):
    """Open-dj vendor adapter Protocol.

    ``name`` is the short adapter identifier (e.g. ``"serato"``).

    ``read`` + ``write`` return their :class:`AdapterReport`; ``read`` also
    returns the decoded :class:`OpenDjLibrary`.
    """

    name: str

    def read(self, source: Path) -> tuple[OpenDjLibrary, AdapterReport]: ...

    def write(
        self, library: OpenDjLibrary, target: Path
    ) -> AdapterReport: ...

    def capabilities(self) -> Capabilities: ...


# ------------------------------------------------------- JCS convenience


def _normalise(value: Any) -> Any:
    """Convert tuples to lists + dataclasses to dicts, recursively.

    :func:`apps.open_dj.canon.to_canonical_bytes` routes through
    ``rfc8785.dumps`` which only accepts plain JSON-serialisable types;
    this normaliser bridges our frozen-dataclass API to that contract.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return _normalise(asdict(value))
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value


def serialize_jcs(obj: Any) -> bytes:
    """Return RFC 8785 JCS bytes for ``obj`` (dataclass, dict, or primitives).

    Thin wrapper around :func:`apps.open_dj.canon.to_canonical_bytes` that
    first normalises tuples and dataclasses so adapter callers don't have
    to think about it.
    """
    from apps.open_dj.canon import to_canonical_bytes

    return to_canonical_bytes(_normalise(obj))


__all__ = [
    "Adapter",
    "AdapterReport",
    "BeatGridPoint",
    "Capabilities",
    "CapabilityField",
    "CuePoint",
    "CueType",
    "OpenDjLibrary",
    "Playlist",
    "SupportLevel",
    "Track",
    "VendorId",
    "Warning",
    "WarningAction",
    "serialize_jcs",
]
