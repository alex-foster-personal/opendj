"""Phase 16 adapter shim for Phase 15 integration.

Phase 16 ships vendor adapters (Serato, Traktor) that target the
``Adapter`` Protocol + typed ``OpenDjLibrary`` dataclasses the ultraplan
Phase 16 CONTEXT assumes Phase 15 will expose.

At the time of Phase 16 execution Phase 15 had shipped the JSON Schema
(``open-dj/schema/v0.2/``) and helper modules ``apps.open_dj.canon`` +
``apps.open_dj.validate`` + ``apps.open_dj.id`` -- but *not* the typed
dataclass layer nor a Protocol definition.

This shim bridges the gap for Phase 16 only:

  * ``Track``, ``Playlist``, ``CuePoint``, ``OpenDjLibrary`` dataclasses
    that serialise to the open-dj v0.2 JSON shape.
  * ``Adapter`` Protocol that Serato + Traktor adapters implement.
  * ``serialize_jcs`` wrapper around Phase 15's ``to_canonical_bytes`` if it
    is importable, with a stdlib fallback otherwise.

TODO(phase-15): when Phase 15 ships typed dataclasses in
``apps.open_dj.types`` (or similar), replace this shim and forward imports
to the Phase 15 module. Search for ``_shim`` imports under ``apps/adapters/``
to find the call sites that need updating.
"""

from __future__ import annotations

from apps.adapters._shim.schema import (
    Adapter,
    AdapterReport,
    BeatGridPoint,
    Capabilities,
    CapabilityField,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
    Warning,
)
from apps.adapters._shim.canon import serialize_jcs

__all__ = [
    "Adapter",
    "AdapterReport",
    "BeatGridPoint",
    "Capabilities",
    "CapabilityField",
    "CuePoint",
    "OpenDjLibrary",
    "Playlist",
    "Track",
    "Warning",
    "serialize_jcs",
]
