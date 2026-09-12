"""Stdlib-only beatgrid payload content digest (nav1-key-record section 5).

Shared by the key producer and the queue dependency cascade. Must not import
``apps.analysis`` (grimp cycle; ``profiles.py`` pulls numpy).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_BEATGRID_DIGEST_FIELDS: tuple[str, ...] = (
    "beats",
    "bpm",
    "octave_reason",
    "tempo_changes",
    "static_grid_untrusted",
)


def beatgrid_record_digest(beatgrid_payload: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of a beatgrid lane payload."""
    canonical = {
        field_name: beatgrid_payload[field_name]
        for field_name in _BEATGRID_DIGEST_FIELDS
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["beatgrid_record_digest"]
