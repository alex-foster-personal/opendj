"""contract_rev: one string that says "engine and client agree".

Computed from the engine's OWN ``app.openapi()`` at startup, never from the
committed openapi.json. Drift between the two is CI's job to catch; runtime
must report what it is actually serving. The WS schema version is folded in
because a client can be wrong about the event envelope while the HTTP
surface is byte-identical.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

WS_SCHEMA_VERSION: str = "ws1"
CONTRACT_REV_CHARS: int = 12


def openapi_digest_input(schema: dict[str, Any]) -> bytes:
    """Canonical bytes: sorted keys, no incidental whitespace."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_contract_rev(schema: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        openapi_digest_input(schema)
        + b"\n"
        + WS_SCHEMA_VERSION.encode("utf-8")
    )
    return digest.hexdigest()[:CONTRACT_REV_CHARS]


__all__ = [
    "CONTRACT_REV_CHARS",
    "WS_SCHEMA_VERSION",
    "compute_contract_rev",
    "openapi_digest_input",
]
