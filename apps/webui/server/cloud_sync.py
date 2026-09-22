"""Syncthing capability probe backing the cloud sync status surface
(cloud-sync-surface, CAT-04).

Syncthing is optional: SYNCTHING_API_URL / SYNCTHING_API_KEY are unset in the
cloud dev environment and on most local checkouts. probe_syncthing_status()
returns None whenever the capability is not configured or unreachable, so the
health endpoint and status chip render an honest "not available" state
instead of fabricated data (see CLAUDE.md house rules). Uses stdlib
urllib rather than httpx, which is a dev/test-only dependency not meant to be
imported by runtime code (see pyproject.toml [project.optional-dependencies]
dev comment).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

_TIMEOUT_SECONDS = 1.5


def _get_json(url: str, api_key: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except (urllib.error.URLError, ValueError, OSError):
        return None


def probe_syncthing_status() -> dict[str, Any] | None:
    """Query a local Syncthing REST API for connection/folder state.

    Returns None when SYNCTHING_API_URL/SYNCTHING_API_KEY are unset or the
    daemon is unreachable, matching the syncthing_status_fn contract consumed
    by routes.health.health() (wrapped in HealthSyncthing there).
    """
    base_url = os.environ.get("SYNCTHING_API_URL", "").rstrip("/")
    api_key = os.environ.get("SYNCTHING_API_KEY", "")
    if not base_url or not api_key:
        return None

    connections = _get_json(f"{base_url}/rest/system/connections", api_key)
    if connections is None:
        return None
    peers_connected = sum(
        1 for c in connections.get("connections", {}).values()
        if isinstance(c, dict) and c.get("connected")
    )

    folder_state = "unknown"
    folder_id = os.environ.get("SYNCTHING_FOLDER_ID", "")
    if folder_id:
        db_status = _get_json(
            f"{base_url}/rest/db/status?folder={folder_id}", api_key,
        )
        if db_status is not None:
            folder_state = db_status.get("state", "unknown")

    return {"peers_connected": peers_connected, "folder_state": folder_state}


__all__ = ["probe_syncthing_status"]
