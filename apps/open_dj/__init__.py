"""open-dj reference implementation (Apache-2.0).

Entry points:

- ``apps.open_dj.id.compute_track_id(meta)`` -- spec section 5 identity chain.
- ``apps.open_dj.canon.to_canonical_bytes(obj)`` -- RFC 8785 JCS bytes.
- ``apps.open_dj.validate.validate_document(obj)`` -- schema validation.
- ``apps.open_dj.diff.diff_documents(a, b)`` -- structural diff on track_id.
- ``apps.open_dj.cli`` -- ``python -m apps.open_dj.cli <command>``.

Typed dataclass layer (Phase 15 + Phase 16 adapters):

- :class:`OpenDjLibrary`, :class:`Track`, :class:`Playlist`
- :class:`CuePoint`, :class:`BeatGridPoint`
- :class:`Capabilities`, :class:`CapabilityField`
- :class:`Warning`, :class:`AdapterReport`
- :class:`Adapter` (runtime-checkable Protocol)
- :func:`serialize_jcs` -- dataclass -> JCS bytes convenience

Adapters live under ``apps.open_dj.adapters.*``.
"""
from __future__ import annotations

from apps.open_dj.schema import (
    Adapter,
    AdapterReport,
    BeatGridPoint,
    Capabilities,
    CapabilityField,
    CuePoint,
    CueType,
    OpenDjLibrary,
    Playlist,
    SupportLevel,
    Track,
    VendorId,
    Warning,
    WarningAction,
    serialize_jcs,
)

SCHEMA_VERSION: str = "0.2"
"""Version of the open-dj spec implemented by this package."""

__all__ = [
    "SCHEMA_VERSION",
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
