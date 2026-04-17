"""Canonical JSON (RFC 8785 JCS) helpers."""
from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def to_canonical_bytes(obj: Any) -> bytes:
    """Return RFC 8785 JCS canonical bytes for ``obj``."""
    return rfc8785.dumps(obj)


def sha256_hex(obj: Any) -> str:
    """Return ``sha256`` hex of canonical bytes of ``obj``."""
    return hashlib.sha256(to_canonical_bytes(obj)).hexdigest()
