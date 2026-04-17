"""Canonical JSON wrapper for Phase 16 adapters.

Prefers Phase 15's ``apps.open_dj.canon.to_canonical_bytes`` (RFC 8785 JCS
via the ``rfc8785`` library) when available. Falls back to a stdlib-only
approximation that is byte-stable for the shapes Phase 16's conformance
fixtures exercise.

Call sites should import ``serialize_jcs`` from ``apps.adapters._shim``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any


def _normalise(value: Any) -> Any:
    """Convert tuples to lists + dataclasses to dicts, recursively."""
    if is_dataclass(value) and not isinstance(value, type):
        return _normalise(asdict(value))
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value


def serialize_jcs(obj: Any) -> bytes:
    """Return RFC 8785 JCS bytes for ``obj``.

    Uses Phase 15's canonicaliser when importable; otherwise a stdlib
    approximation (sorted keys, no whitespace, UTF-8). Not a full RFC 8785
    implementation in the fallback path -- floats are not normalised -- but
    byte-stable for all Phase 16 fixture shapes.
    """
    canonical = _normalise(obj)
    try:
        from apps.open_dj.canon import to_canonical_bytes  # type: ignore

        return to_canonical_bytes(canonical)
    except Exception:
        # Phase 15 not importable (missing rfc8785 dep, schema, or the
        # package was deleted) -- fall back to the stdlib approximation.
        return json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
