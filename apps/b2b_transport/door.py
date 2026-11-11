"""B2BNET-04: one door message shape for every transport, served by the LAN door's own handlers.

The LAN door (``apps.b2b_pairing.b2b_door``) is an HTTP server; the internet door carries the
same requests inside sealed frames. Both answer through ``b2b_door.answer_get`` /
``answer_post`` / ``answer_relay``, so a door message means the same thing on either path and
a route added to the LAN door is reachable over the internet with no second table.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal

from apps.b2b_pairing.b2b_door import (
    BOOTH_PREFIX,
    RELAY_PREFIX,
    PairingHost,
    answer_booth,
    answer_get,
    answer_post,
    answer_relay,
)

Method = Literal["GET", "POST"]
#: Where an internet door request "comes from" in the LAN handlers' ``source_ip`` slot.
INTERNET_SOURCE = "internet-relay"


@dataclass(frozen=True)
class DoorRequest:
    method: Method
    path: str
    query: str = ""
    body: bytes = b""

    def to_wire(self, request_id: int) -> dict[str, Any]:
        return {"k": "req", "id": request_id, "method": self.method, "path": self.path, "query": self.query,
                "body": base64.b64encode(self.body).decode()}

    @classmethod
    def from_wire(cls, frame: dict[str, Any]) -> DoorRequest:
        method = frame["method"]
        if method not in ("GET", "POST"):
            raise ValueError(f"door method {method!r} is not GET or POST")
        return cls(method=method, path=str(frame["path"]), query=str(frame.get("query", "")),
                   body=base64.b64decode(frame.get("body", "")))


@dataclass(frozen=True)
class DoorAnswer:
    status: int
    content_type: str
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def to_wire(self, request_id: int) -> dict[str, Any]:
        return {"k": "res", "id": request_id, "status": self.status, "content_type": self.content_type,
                "body": base64.b64encode(self.body).decode()}

    @classmethod
    def from_wire(cls, frame: dict[str, Any]) -> DoorAnswer:
        return cls(status=int(frame["status"]), content_type=str(frame["content_type"]),
                   body=base64.b64decode(frame["body"]))


def serve_door_request(host: PairingHost, engine_url: str, token: str, request: DoorRequest) -> DoorAnswer:
    """Answer ``request`` exactly as the LAN door would for a peer holding ``token``.

    The internet session itself proves the pairing (its Noise PSK is the token), so the
    bearer header the LAN door expects is supplied here rather than carried on the wire.
    """
    bearer = f"Bearer {token}"
    if request.method == "GET":
        status, body, content_type = answer_get(host, engine_url, request.path, request.query, bearer)
    elif request.path.startswith(BOOTH_PREFIX):
        status, body, content_type = answer_booth(host, request.path, request.body, bearer)
    elif request.path.startswith(RELAY_PREFIX):
        status, body, content_type = answer_relay(host, request.path, request.body)
    else:
        status, body, content_type = answer_post(host, request.path, request.body, INTERNET_SOURCE)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    return DoorAnswer(status=status, content_type=content_type, body=data)
