"""``opendj requests``: crowd request session and list (CROWDREQ-10749).

    opendj requests open --playlist <id> [--text] [--cooldown 180]
    opendj requests close
    opendj requests list [--status new]
    opendj requests dismiss <id>
    opendj requests loaded <id>
    opendj requests clear

Same documents as the /api/v1/requests routes, and the MCP tools registered
from :mod:`apps.opendj_cli.mcp_requests_tools`.
"""

from __future__ import annotations

import argparse
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
USAGE = (
    "usage: opendj requests open --playlist <id> [--text] [--cooldown 180] | "
    "close | list [--status new] | dismiss <id> | loaded <id> | clear"
)


class RequestsFailed(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, _url(base_url, path), json=json_body, params=params)
    if not response.is_success:
        raise RequestsFailed(response.status_code, response.text)
    return response.json()


def get_session(base_url: str) -> dict[str, Any]:
    return _request(base_url, "GET", "/api/v1/requests/session")


def put_session(base_url: str, body: dict[str, Any]) -> dict[str, Any]:
    return _request(base_url, "PUT", "/api/v1/requests/session", json_body=body)


def list_requests(base_url: str, status: str = "new") -> dict[str, Any]:
    return _request(base_url, "GET", "/api/v1/requests", params={"status": status})


def dismiss_request(base_url: str, request_id: str) -> dict[str, Any]:
    return _request(base_url, "POST", f"/api/v1/requests/{request_id}:dismiss")


def loaded_request(base_url: str, request_id: str) -> dict[str, Any]:
    return _request(base_url, "POST", f"/api/v1/requests/{request_id}:loaded")


def clear_requests(base_url: str) -> dict[str, Any]:
    return _request(base_url, "DELETE", "/api/v1/requests")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opendj requests", add_help=False)
    sub = parser.add_subparsers(dest="verb")
    opened = sub.add_parser("open", add_help=False)
    opened.add_argument("--playlist", default=None)
    opened.add_argument("--text", action="store_true")
    opened.add_argument("--cooldown", type=int, default=180)
    sub.add_parser("close", add_help=False)
    listed = sub.add_parser("list", add_help=False)
    listed.add_argument("--status", default="new")
    dismissed = sub.add_parser("dismiss", add_help=False)
    dismissed.add_argument("request_id")
    loaded = sub.add_parser("loaded", add_help=False)
    loaded.add_argument("request_id")
    sub.add_parser("clear", add_help=False)
    return parser


def _dispatch(base_url: str, args: argparse.Namespace) -> dict[str, Any]:
    if args.verb == "open":
        if args.cooldown < 1:
            raise ValueError("cooldown must be a positive integer")
        return put_session(
            base_url,
            {
                "open": True,
                "catalog_playlist_id": args.playlist,
                "free_text_allowed": bool(args.text),
                "cooldown_s": args.cooldown,
            },
        )
    if args.verb == "close":
        return put_session(base_url, {"open": False})
    if args.verb == "list":
        return list_requests(base_url, args.status)
    if args.verb == "dismiss":
        return dismiss_request(base_url, args.request_id)
    if args.verb == "loaded":
        return loaded_request(base_url, args.request_id)
    return clear_requests(base_url)


def _session_line(doc: dict[str, Any]) -> str:
    catalog = doc.get("catalog_playlist_id") or "-"
    text = "on" if doc.get("free_text_allowed") else "off"
    state = "open" if doc.get("open") else "closed"
    return f"requests {state} catalog={catalog} text={text} cooldown={doc.get('cooldown_s')}"


def _request_label(row: dict[str, Any]) -> str:
    if row.get("text"):
        return str(row["text"])
    artist = row.get("artist") or ""
    title = row.get("title") or row.get("stable_id") or ""
    return f"{artist} {title}".strip()


def _list_lines(doc: dict[str, Any]) -> str:
    rows = doc.get("requests") or []
    if not rows:
        return "no requests"
    return "\n".join(
        f"{row.get('id')}  {row.get('status')}  {row.get('votes')}  {row.get('requester_name')}  {_request_label(row)}"
        for row in rows
    )


def _row_line(doc: dict[str, Any]) -> str:
    label = doc.get("text") or doc.get("stable_id") or doc.get("id")
    return f"{doc.get('id')}  {doc.get('status')}  {label}"


def text_summary(verb: str, doc: dict[str, Any]) -> str:
    if verb in {"open", "close"}:
        return _session_line(doc)
    if verb == "list":
        return _list_lines(doc)
    if verb == "clear":
        return f"removed {doc.get('removed', 0)}"
    return _row_line(doc)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    parsed, unknown = _parser().parse_known_args(list(rest))
    if unknown or parsed.verb is None:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    if parsed.verb == "open" and not parsed.playlist:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    try:
        target = resolve_backend_base_url(lock_path=lock)
        doc = _dispatch(target.base_url, parsed)
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except RequestsFailed as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    print(json.dumps(doc, indent=2, sort_keys=True) if as_json else text_summary(parsed.verb, doc))
    return EXIT_CONFIRMED
