"""``opendj session``: the session played set (SESSION-PLAYED-01).

    opendj session played     tracks this session already played (the green library rows)
    opendj session resume     continue a session flagged expired (idle over 12 h)
    opendj session new        start a new session; the old one is archived, never deleted

Same documents as ``GET /api/v1/session/played``, ``POST /api/v1/session/resume``
and ``POST /api/v1/session/new``, and the MCP tools ``session_played``,
``session_resume`` and ``session_new``.

-Claude
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

REQUEST_TIMEOUT_S = 15.0
USAGE = "usage: opendj session played | resume | new"
VERB_REQUESTS: dict[str, tuple[str, str]] = {
    "played": ("GET", "/api/v1/session/played"),
    "resume": ("POST", "/api/v1/session/resume"),
    "new": ("POST", "/api/v1/session/new"),
}


class SessionRequestFailed(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def request_session(base_url: str, verb: str) -> dict[str, Any]:
    """One session verb against a running engine; raises on any non-2xx."""
    method, path = VERB_REQUESTS[verb]
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, f"{base_url.rstrip('/')}{path}")
    if not response.is_success:
        raise SessionRequestFailed(response.status_code, response.text)
    return response.json()


def _when(ms: int | None) -> str:
    return "never" if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone().strftime("%a %d %b %H:%M")


def text_summary(doc: dict[str, Any]) -> str:
    state = "EXPIRED (resume or new)" if doc["expired"] else "live"
    head = (
        f"session {doc['session_id'] or 'none'}: {doc['count']} played, {state}, "
        f"started {_when(doc['started_at_ms'])}, last activity {_when(doc['last_activity_ms'])}"
    )
    return "\n".join([head, *doc["played_ids"]])


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if len(rest) != 1 or rest[0] not in VERB_REQUESTS:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    try:
        target = resolve_backend_base_url(lock_path=lock)
        doc = request_session(target.base_url, rest[0])
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except SessionRequestFailed as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    print(json.dumps(doc, indent=2, sort_keys=True) if as_json else text_summary(doc))
    return EXIT_CONFIRMED
