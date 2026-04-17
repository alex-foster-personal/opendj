"""Typed dataclass layer for Phase 16 adapters.

These mirror the open-dj v0.2 JSON Schema shape at
``open-dj/schema/v0.2/open-dj.schema.json`` (shipped by Phase 15) just enough
for the Serato + Traktor adapters to round-trip a library. The types in this
shim are deliberately a small subset of v0.2 (the one Phase 16's conformance
harness exercises) so wire-up to the eventual Phase 15 typed layer is a
find-and-replace.

TODO(phase-15): replace with Phase 15's typed dataclasses once shipped.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


# ----------------------------------------------------------- track types


@dataclass(frozen=True)
class BeatGridPoint:
    """One beat-anchor: a position (ms) + BPM locked from that position."""

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
    type: Literal["hot", "memory", "loop", "load", "fade_in", "fade_out", "grid"]
    name: str = ""
    color_rgb: int | None = None
    length_ms: int | None = None


@dataclass(frozen=True)
class Track:
    """open-dj v0.2 Track (minimal subset for Phase 16 adapters)."""

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
    name: str
    track_ids: tuple[str, ...]
    children: tuple["Playlist", ...] = ()


@dataclass(frozen=True)
class OpenDjLibrary:
    """Canonical open-dj library object. Adapter ``read()`` returns this."""

    version: str
    tracks: tuple[Track, ...]
    playlists: tuple[Playlist, ...] = ()
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------- capabilities


@dataclass(frozen=True)
class CapabilityField:
    """One row of the capability table."""

    field: str
    supports: Literal["lossless", "lossy", "unsupported"]
    rationale: str = ""


@dataclass(frozen=True)
class Capabilities:
    """Per-adapter capability descriptor (open-dj §6 + §7 lossy lanes)."""

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
    """Structured adapter warning (open-dj §6 lossy-lane reporting)."""

    field: str
    track_id: str
    action: Literal["dropped", "downgraded", "stored_as_extension", "synthesised"]
    reason: str


@dataclass
class AdapterReport:
    """Mutable report returned alongside ``read()`` / ``write()``."""

    warnings: list[Warning] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, by: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + by

    def warn(
        self,
        *,
        field: str,
        track_id: str,
        action: Literal["dropped", "downgraded", "stored_as_extension", "synthesised"],
        reason: str,
    ) -> None:
        self.warnings.append(
            Warning(field=field, track_id=track_id, action=action, reason=reason)
        )


# ---------------------------------------------------------- Adapter API


@runtime_checkable
class Adapter(Protocol):
    """open-dj vendor adapter Protocol (Phase 16 shim).

    ``name`` is the short adapter identifier (e.g. ``"serato"``).

    ``read`` + ``write`` return their ``AdapterReport``; ``read`` also
    returns the decoded ``OpenDjLibrary``.
    """

    name: str

    def read(self, source: Path) -> tuple[OpenDjLibrary, AdapterReport]: ...

    def write(
        self, library: OpenDjLibrary, target: Path
    ) -> AdapterReport: ...

    def capabilities(self) -> Capabilities: ...
