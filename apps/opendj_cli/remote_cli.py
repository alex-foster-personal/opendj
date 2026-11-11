"""``opendj remote``: phone-remote listener start, stop, status, rotate, forget.

    opendj remote start [--port N]
    opendj remote stop
    opendj remote status
    opendj remote rotate
    opendj remote forget <device-id>

Same documents as ``/api/v1/performance/remote-control`` (issues #10434, #10443).
Status prints ``reopened at boot`` when the engine restored the listener.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

REQUEST_TIMEOUT_S = 15.0
BASE = "/api/v1/performance/remote-control"
USAGE = "usage: opendj remote start [--port N] | stop | status | rotate | forget <device-id>"


class RemoteRequestFailed(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _request(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, url, json=body)
    if not response.is_success:
        raise RemoteRequestFailed(response.status_code, response.text)
    return response.json()


def text_status(doc: dict[str, Any]) -> str:
    status = doc.get("status", "off")
    lines: list[str] = [f"remote-control: {status}"]
    if doc.get("reopened_at_boot"):
        lines.append("reopened at boot")
    if status == "listening":
        address = doc.get("address") or "?"
        port = doc.get("port")
        code = doc.get("code") or "?"
        lines.append(f"{address}:{port}  code {code}")
    if doc.get("last_error"):
        lines.append(f"last_error: {doc['last_error']}")
    if doc.get("address_error"):
        lines.append(f"address_error: {doc['address_error']}")
    for device in doc.get("devices") or []:
        lines.append(
            f"device {device['device_id']}: {device['device_name']} "
            f"(paired {device['paired_at']}, last seen {device['last_seen']})"
        )
    return "\n".join(lines)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if not rest:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    verb = rest[0]
    try:
        target = resolve_backend_base_url(lock_path=lock)
        base = target.base_url
        if verb == "start":
            port: int | None = None
            tokens = list(rest[1:])
            while tokens:
                flag = tokens.pop(0)
                if flag == "--port":
                    if not tokens:
                        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
                    port = int(tokens.pop(0))
                else:
                    return _fail(as_json, "usage", USAGE, EXIT_FAILED)
            body: dict[str, Any] = {} if port is None else {"port": port}
            doc = _request(base, "POST", f"{BASE}/start", body)
        elif verb == "stop":
            if len(rest) != 1:
                return _fail(as_json, "usage", USAGE, EXIT_FAILED)
            doc = _request(base, "POST", f"{BASE}/stop", {})
        elif verb == "status":
            if len(rest) != 1:
                return _fail(as_json, "usage", USAGE, EXIT_FAILED)
            doc = _request(base, "GET", BASE)
        elif verb == "rotate":
            if len(rest) != 1:
                return _fail(as_json, "usage", USAGE, EXIT_FAILED)
            doc = _request(base, "POST", f"{BASE}/rotate-code", {})
        elif verb == "forget":
            if len(rest) != 2:
                return _fail(as_json, "usage", USAGE, EXIT_FAILED)
            doc = _request(base, "DELETE", f"{BASE}/devices/{rest[1]}")
        else:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except (RemoteRequestFailed, httpx.HTTPError, ValueError) as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    print(json.dumps(doc, indent=2, sort_keys=True) if as_json else text_status(doc))
    return EXIT_CONFIRMED
