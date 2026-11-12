"""``opendj remote``: phone-remote listener start, stop, status, rotate, forget.

    opendj remote start [--port N]
    opendj remote stop
    opendj remote status
    opendj remote rotate
    opendj remote forget <device-id>
    opendj remote qr [--target remote|request|display] [--out FILE]

Same documents as ``/api/v1/performance/remote-control`` (issues #10434, #10443).
Status prints ``reopened at boot`` when the engine restored the listener.
MCP tools ``remote_*`` are built from ``REMOTE_VERBS``.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

REQUEST_TIMEOUT_S = 15.0
BASE = "/api/v1/performance/remote-control"
USAGE = "usage: opendj remote start [--port N] | stop | status | rotate | forget <device-id> | qr [--target remote|request|display] [--out FILE]"


@dataclass(frozen=True)
class RemoteVerb:
    name: str
    method: str
    path: str
    read_only: bool
    destructive: bool
    summary: str


REMOTE_VERBS: tuple[RemoteVerb, ...] = (
    RemoteVerb(
        "start",
        "POST",
        "/start",
        False,
        False,
        "Open the phone-remote LAN listener (port 47810, 0 for ephemeral); idempotent.",
    ),
    RemoteVerb(
        "stop",
        "POST",
        "/stop",
        False,
        False,
        "Close the listener; paired devices survive; idempotent.",
    ),
    RemoteVerb(
        "status",
        "GET",
        "",
        True,
        False,
        "Status, pairing code, address:port and paired devices (never tokens).",
    ),
    RemoteVerb(
        "rotate",
        "POST",
        "/rotate-code",
        False,
        False,
        "Issue a new pairing code without stopping.",
    ),
    RemoteVerb(
        "forget",
        "DELETE",
        "/devices/{device_id}",
        False,
        True,
        "Forget one paired device; its token stops working.",
    ),
)
REMOTE_VERB_NAMES = tuple(verb.name for verb in REMOTE_VERBS)
_VERBS_BY_NAME = {verb.name: verb for verb in REMOTE_VERBS}


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


def call_verb(
    base_url: str,
    name: str,
    *,
    port: int | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    verb = _VERBS_BY_NAME[name]
    if verb.name == "forget" and not device_id:
        raise ValueError("forget requires a device id")
    path = BASE + verb.path.format(device_id=urllib.parse.quote(device_id or "", safe=""))
    if verb.method in {"GET", "DELETE"}:
        body = None
    elif port is None:
        body = {}
    else:
        body = {"port": port}
    return _request(base_url, verb.method, path, body)


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


def _encode_qr_url(*, target: str, address: str, port: int, code: str) -> str:
    base = f"http://{address}:{port}"
    if target == "remote":
        return f"{base}/remote#code={code}"
    if target == "request":
        return f"{base}/request"
    if target == "display":
        return f"{base}/now-playing"
    raise ValueError(f"unknown QR target {target!r}")


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if not rest:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    verb_name = rest[0]
    if verb_name == "qr":
        qr_target = "remote"
        out: Path | None = None
        tokens = list(rest[1:])
        while tokens:
            flag = tokens.pop(0)
            if flag == "--target":
                if not tokens:
                    return _fail(as_json, "usage", USAGE, EXIT_FAILED)
                qr_target = tokens.pop(0)
            elif flag == "--out":
                if not tokens:
                    return _fail(as_json, "usage", USAGE, EXIT_FAILED)
                out = Path(tokens.pop(0))
            else:
                return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        if qr_target not in ("remote", "request", "display"):
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        try:
            engine = resolve_backend_base_url(lock_path=lock)
            base = engine.base_url
            svg_url = f"{base.rstrip('/')}{BASE}/qr.svg?target={qr_target}"
            with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
                response = client.get(svg_url)
            if not response.is_success:
                raise RemoteRequestFailed(response.status_code, response.text)
            if out is not None:
                out.write_text(response.text, encoding="utf-8")
                doc = {"target": qr_target, "out": str(out), "bytes": len(response.content)}
            else:
                status = _request(base, "GET", BASE)
                if status.get("status") != "listening" or not status.get("address") or not status.get("port"):
                    return _fail(
                        as_json,
                        "not_listening",
                        "remote-control listener is not listening",
                        EXIT_FAILED,
                    )
                encoded = _encode_qr_url(
                    target=qr_target,
                    address=status["address"],
                    port=int(status["port"]),
                    code=str(status.get("code") or ""),
                )
                if as_json:
                    doc = {"target": qr_target, "url": encoded}
                else:
                    print(encoded)
                    return EXIT_CONFIRMED
        except (UsageError, EngineNotRunning) as error:
            return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
        except (RemoteRequestFailed, httpx.HTTPError, ValueError) as error:
            return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
        if as_json:
            print(json.dumps(doc, indent=2, sort_keys=True))
        elif out is not None:
            print(doc["out"])
        return EXIT_CONFIRMED
    if verb_name not in _VERBS_BY_NAME:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    port: int | None = None
    device_id: str | None = None
    if verb_name == "start":
        tokens = list(rest[1:])
        while tokens:
            flag = tokens.pop(0)
            if flag == "--port":
                if not tokens:
                    return _fail(as_json, "usage", USAGE, EXIT_FAILED)
                try:
                    port = int(tokens.pop(0))
                except ValueError:
                    return _fail(as_json, "usage", USAGE, EXIT_FAILED)
            else:
                return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    elif verb_name in {"stop", "status", "rotate"}:
        if len(rest) != 1:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    elif verb_name == "forget":
        if len(rest) != 2:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        device_id = rest[1]
    try:
        target = resolve_backend_base_url(lock_path=lock)
        doc = call_verb(target.base_url, verb_name, port=port, device_id=device_id)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except (RemoteRequestFailed, httpx.HTTPError, ValueError) as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    print(json.dumps(doc, indent=2, sort_keys=True) if as_json else text_status(doc))
    return EXIT_CONFIRMED
