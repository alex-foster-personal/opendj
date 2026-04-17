"""Phase 16 adapter shim for the Phase 15 typed layer.

Phase 16 vendor adapters (Serato, Traktor) target an ``Adapter`` Protocol
+ typed ``OpenDjLibrary`` dataclasses. Phase 15 shipped the JSON Schema
(``open-dj/schema/v0.2/``), canonical-bytes helper
(``apps.open_dj.canon``), identity chain (``apps.open_dj.id``), and
validator (``apps.open_dj.validate``), but **not yet** a typed dataclass
layer or an ``Adapter`` Protocol.

What this shim still provides (Phase 15 has no equivalent shipped):

  * ``Track``, ``Playlist``, ``CuePoint``, ``OpenDjLibrary``,
    ``Capabilities``, ``CapabilityField``, ``Warning``, ``AdapterReport``
    dataclasses matching the open-dj v0.2 JSON shape.
  * ``Adapter`` Protocol implemented by Serato + Traktor adapters.

What is forwarded to Phase 15:

  * ``serialize_jcs`` now composes a local ``_normalise`` (tuples -> lists,
    dataclasses -> dicts) on top of :func:`apps.open_dj.canon.to_canonical_bytes`.
    The stdlib JSON fallback from the original shim was removed: Phase 15's
    ``rfc8785`` dependency is a hard requirement now.

TODO(phase-15): when Phase 15 lands typed dataclasses + Adapter Protocol
(see ``apps/open_dj/schema_loader.py`` vs. the JSON Schema at
``open-dj/schema/v0.2/open-dj.schema.json``), retire ``_shim/schema.py`` and
forward the imports to the Phase 15 module. Grep for ``from apps.adapters._shim``
under ``apps/adapters/`` + ``tests/`` for the call sites to update. Delta
gap-fill (see commit log) already retired ``_shim/canon.py``.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from apps.open_dj.canon import to_canonical_bytes

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


def _normalise(value: Any) -> Any:
    """Convert tuples to lists + dataclasses to dicts, recursively.

    Required because Phase 15's :func:`to_canonical_bytes` routes through
    ``rfc8785.dumps`` which only accepts plain JSON-serialisable types.
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

    Delegates to :func:`apps.open_dj.canon.to_canonical_bytes` after
    normalising tuples + dataclasses. The old stdlib fallback path has been
    retired now that Phase 15's rfc8785 dep ships with the package.
    """
    return to_canonical_bytes(_normalise(obj))

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
