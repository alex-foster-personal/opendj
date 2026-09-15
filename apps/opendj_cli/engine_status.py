"""Shared engine status probe used by MCP ``status`` and ``opendj status``."""

from __future__ import annotations

from typing import Any

import httpx

from apps.opendj_cli.origin import EngineOrigin, unreachable

PROBE_TIMEOUT_S = 3.0


def probe_json(origin: EngineOrigin, path: str) -> Any:
    """GET *path* on *origin*; return parsed JSON, text, or an UNREACHABLE string."""
    url = f"{origin.base_url}{path}"
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S) as client:
            response = client.get(url)
    except httpx.TransportError as error:
        raise unreachable(origin, error) from error
    if response.status_code != 200:
        return f"UNREACHABLE: {response.status_code} {response.text[:200]}"
    try:
        return response.json()
    except ValueError:
        return response.text


def build_engine_status(origin: EngineOrigin) -> dict[str, Any]:
    return {
        "lock_path": str(origin.lock_path),
        "origin": origin.base_url,
        "pid": origin.pid,
        "role": origin.role,
        "health": probe_json(origin, "/api/v1/health"),
        "build_info": probe_json(origin, "/api/v1/build-info"),
    }
