"""``opendj set``: playlist set tools and portable set export/import.

    opendj set summary <playlist_id>
    opendj set target <playlist_id> <minutes>|clear
    opendj set skip <playlist_id> <stable_id> [off]
    opendj set export <session_id> [--out FILE]
    opendj set import FILE

Playlist commands mirror ``/api/v1/playlists/{id}/set-*``. Portable export/import mirror
``GET /api/sets/{session_id}/export`` and ``POST /api/sets/import``.

-Claude
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

PLAYLISTS_PATH = "/api/v1/playlists"
SETS_PATH = "/api/sets"
REQUEST_TIMEOUT_S = 30.0
USAGE = (
    "usage: opendj set summary <playlist_id> | target <playlist_id> <minutes>|clear"
    " | skip <playlist_id> <stable_id> [off]"
    " | export <session_id> [--out FILE] | import FILE"
)


def request_for(args: Sequence[str]) -> tuple[str, str, dict[str, Any] | None]:
    """(method, path, body) for one ``opendj set`` call; raises ValueError on bad usage."""
    if len(args) == 2 and args[0] == "summary":
        return "GET", f"{PLAYLISTS_PATH}/{args[1]}/set-summary", None
    if len(args) == 3 and args[0] == "target":
        target_s = None if args[2] == "clear" else float(args[2]) * 60.0
        if target_s is not None and target_s <= 0:
            raise ValueError("target minutes must be positive")
        return "PUT", f"{PLAYLISTS_PATH}/{args[1]}/set-target", {"target_s": target_s}
    if len(args) in (3, 4) and args[0] == "skip":
        if len(args) == 4 and args[3] != "off":
            raise ValueError(f"expected 'off', got {args[3]!r}")
        return "PUT", f"{PLAYLISTS_PATH}/{args[1]}/set-skips/{args[2]}", {"skipped": len(args) == 3}
    raise ValueError(USAGE)


def _text(payload: dict[str, Any]) -> str:
    lines = [f"{payload['playlist_id']}: {payload['readout']} ({payload['skipped']} skipped)"]
    lines += [
        f"key clash {c['from_key']} -> {c['to_key']} ({c['steps']} steps){' GROSS' if c['gross'] else ''}"
        f"  {c['from_id']} -> {c['to_id']}"
        for c in payload["clashes"]
    ]
    return "\n".join(lines)


def _http_request(
    *,
    lock: Path | None,
) -> tuple[httpx.Client, str]:
    target = resolve_backend_base_url(lock_path=lock)
    client = httpx.Client(timeout=REQUEST_TIMEOUT_S)
    base = target.base_url.rstrip("/")
    return client, base


def _fail_response(
    as_json: bool,
    response: httpx.Response,
    *,
    method: str,
    path: str,
) -> int:
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict) and detail.get("code"):
            return _fail(as_json, str(detail["code"]), str(detail.get("message", "")), EXIT_FAILED)
    return _fail(
        as_json,
        "request_failed",
        f"{method} {path} returned {response.status_code}: {response.text}",
        EXIT_FAILED,
    )


def _parse_portable_args(rest: Sequence[str]) -> argparse.Namespace | None:
    parser = argparse.ArgumentParser(prog="opendj set", add_help=False, exit_on_error=False)
    sub = parser.add_subparsers(dest="command")
    export_p = sub.add_parser("export")
    export_p.add_argument("session_id")
    export_p.add_argument("--out", dest="out_path", default=None)
    import_p = sub.add_parser("import")
    import_p.add_argument("document_path")
    try:
        return parser.parse_args(list(rest))
    except (SystemExit, argparse.ArgumentError):
        return None


def _portable_export(ns: argparse.Namespace, *, as_json: bool, lock: Path | None) -> int:
    path = f"{SETS_PATH}/{quote(ns.session_id, safe='')}/export"
    try:
        client, base = _http_request(lock=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        response = client.get(f"{base}{path}")
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    finally:
        client.close()
    if not response.is_success:
        return _fail_response(as_json, response, method="GET", path=path)
    doc = response.json()
    if ns.out_path:
        out = Path(ns.out_path)
        out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        summary = {
            "ok": True,
            "session_id": ns.session_id,
            "out": str(out),
            "events": len(doc.get("events", [])),
        }
        print(json.dumps(summary, indent=2, sort_keys=True) if as_json else f"wrote {out} ({summary['events']} events)")
    else:
        print(json.dumps(doc, indent=2, sort_keys=True) if as_json else json.dumps(doc))
    return EXIT_CONFIRMED


def _portable_import(ns: argparse.Namespace, *, as_json: bool, lock: Path | None) -> int:
    doc_path = Path(ns.document_path)
    if not doc_path.is_file():
        return _fail(as_json, "usage", f"file not found: {doc_path}", EXIT_FAILED)
    try:
        document = json.loads(doc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return _fail(as_json, "usage", f"invalid JSON in {doc_path}: {error}", EXIT_FAILED)
    path = f"{SETS_PATH}/import"
    try:
        client, base = _http_request(lock=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        response = client.post(f"{base}{path}", json=document)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    finally:
        client.close()
    if not response.is_success:
        return _fail_response(as_json, response, method="POST", path=path)
    payload = response.json()
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else payload.get("message", json.dumps(payload)))
    return EXIT_CONFIRMED


def _run_portable(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    ns = _parse_portable_args(rest)
    if ns is None or ns.command == "export":
        if ns is None:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        return _portable_export(ns, as_json=as_json, lock=lock)
    if ns.command == "import":
        return _portable_import(ns, as_json=as_json, lock=lock)
    return _fail(as_json, "usage", USAGE, EXIT_FAILED)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if rest and rest[0] in {"export", "import"}:
        return _run_portable(rest, as_json=as_json, lock=lock)
    try:
        method, path, body = request_for(list(rest))
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    try:
        client, base = _http_request(lock=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        response = client.request(method, f"{base}{path}", json=body)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    finally:
        client.close()
    if not response.is_success:
        return _fail(
            as_json,
            "request_failed",
            f"{method} {path} returned {response.status_code}: {response.text}",
            EXIT_FAILED,
        )
    payload = response.json()
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else _text(payload))
    return EXIT_CONFIRMED
