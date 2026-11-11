"""B2BNET-04: the LAN adapter. The existing LAN door, unchanged, behind the shared door shape.

The LAN server stays ``apps.b2b_pairing.b2b_door.Door`` (re-exported here, not moved, so every
LAN caller and test keeps its import). This module adds only the client half in the same
``DoorRequest`` / ``DoorAnswer`` terms the internet adapter uses, so a caller can send a door
message without caring which path carries it.
"""

from __future__ import annotations

import httpx

from apps.b2b_pairing.b2b_door import Door
from apps.b2b_transport.door import DoorAnswer, DoorRequest

__all__ = ["Door", "lan_door_request"]


def _peer_url(peer_url: str, path: str, query: str) -> str:
    return f"{peer_url}{path}" + (f"?{query}" if query else "")


def lan_door_request(peer_url: str, token: str, request: DoorRequest, timeout: float) -> DoorAnswer:
    """One door message to a LAN partner at ``peer_url`` (``http://host:port``), bearer ``token``."""
    url = _peer_url(peer_url, request.path, request.query)
    response = httpx.request(request.method, url, content=request.body or None,
                             headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    return DoorAnswer(status=response.status_code, content_type=response.headers.get("content-type", ""),
                      body=response.content)
