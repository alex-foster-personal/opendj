"""Crowd request store (CROWDREQ-10749): one session and a request list in state.db.

Two foreign-authority tables on the shared connection, created on first use
the same way set_targets are. Machine-local performance state: not in the
sync set. requester_key is a sha256 the public listener computes; this module
never stores an address or cookie.

Pure functions: ensure_schema, get_session, set_session, add_request, vote,
set_status, list_requests, clear_requests. The engine route publishes
crowd_request.added after the write commits, so a subscriber can refetch
the new row.

The public listener (#10445) computes requester_key with
requester_fingerprint, filters free text with the bundled denylist, and
uses VoteLedger so one requester_key can vote once per request.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

DEFAULT_COOLDOWN_S = 180
STATUSES = ("new", "loaded", "dismissed")
KINDS = ("track", "text")
NAME_MAX = 24
TEXT_MAX = 140
ADDED_TOPIC = "crowd_request.added"

Status = Literal["new", "loaded", "dismissed"]
Kind = Literal["track", "text"]

_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS crowd_request_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    open INTEGER NOT NULL CHECK (open IN (0, 1)),
    catalog_playlist_id TEXT,
    free_text_allowed INTEGER NOT NULL CHECK (free_text_allowed IN (0, 1)),
    cooldown_s INTEGER NOT NULL CHECK (cooldown_s > 0),
    opened_at TEXT
)
"""

_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS crowd_requests (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('track', 'text')),
    stable_id TEXT,
    text TEXT,
    requester_name TEXT NOT NULL,
    requester_key TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0 CHECK (votes >= 0),
    status TEXT NOT NULL CHECK (status IN ('new', 'loaded', 'dismissed')),
    status_at TEXT NOT NULL
)
"""

_REQUESTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_crowd_requests_status_created
    ON crowd_requests(status, created_at DESC)
"""


class SessionError(ValueError):
    """A session update the store will not apply."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RequestError(ValueError):
    """A request body the store will not accept."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CrowdSession:
    open: bool
    catalog_playlist_id: str | None
    free_text_allowed: bool
    cooldown_s: int
    opened_at: str | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CrowdRequest:
    id: str
    created_at: str
    kind: str
    stable_id: str | None
    text: str | None
    requester_name: str
    requester_key: str
    votes: int
    status: str
    status_at: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


_DENYLIST_PATH = Path(__file__).with_name("crowd_requests_denylist.txt")


def requester_fingerprint(cookie: str, source_ip: str) -> str:
    """sha256 of cookie + source IP. Callers must not persist the inputs."""
    return hashlib.sha256(f"{cookie}{source_ip}".encode()).hexdigest()


@cache
def load_denylist(path: Path | None = None) -> tuple[str, ...]:
    target = path if path is not None else _DENYLIST_PATH
    stems: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip().lower()
        if text:
            stems.append(text)
    return tuple(stems)


def text_is_filtered(message: str, stems: Sequence[str] | None = None) -> bool:
    """True when a denylist stem matches at a word boundary (case-insensitive)."""
    haystack = message.lower()
    chosen = load_denylist() if stems is None else stems
    return any(stem and re.search(rf"\b{re.escape(stem)}", haystack) for stem in chosen)


class VoteLedger:
    """In-process vote-once book keyed by (request_id, requester_key)."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def already_voted(self, request_id: str, requester_key: str) -> bool:
        return (request_id, requester_key) in self._seen

    def mark(self, request_id: str, requester_key: str) -> bool:
        key = (request_id, requester_key)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _default_session() -> CrowdSession:
    return CrowdSession(
        open=False,
        catalog_playlist_id=None,
        free_text_allowed=False,
        cooldown_s=DEFAULT_COOLDOWN_S,
        opened_at=None,
    )


def _session_from_row(row: sqlite3.Row | tuple[Any, ...]) -> CrowdSession:
    open_flag, catalog, free_text, cooldown_s, opened_at = row
    return CrowdSession(
        open=bool(open_flag),
        catalog_playlist_id=catalog,
        free_text_allowed=bool(free_text),
        cooldown_s=int(cooldown_s),
        opened_at=opened_at,
    )


def _request_from_row(row: tuple[Any, ...]) -> CrowdRequest:
    (
        request_id,
        created_at,
        kind,
        stable_id,
        text,
        requester_name,
        requester_key,
        votes,
        status,
        status_at,
    ) = row
    return CrowdRequest(
        id=str(request_id),
        created_at=str(created_at),
        kind=str(kind),
        stable_id=stable_id,
        text=text,
        requester_name=str(requester_name),
        requester_key=str(requester_key),
        votes=int(votes),
        status=str(status),
        status_at=str(status_at),
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SESSION_DDL)
    conn.execute(_REQUESTS_DDL)
    conn.execute(_REQUESTS_INDEX)


def get_session(conn: sqlite3.Connection) -> CrowdSession:
    row = conn.execute(
        "SELECT open, catalog_playlist_id, free_text_allowed, cooldown_s, opened_at "
        "FROM crowd_request_session WHERE id = 1"
    ).fetchone()
    return _default_session() if row is None else _session_from_row(row)


def _catalog_value(current: CrowdSession, catalog_playlist_id: str | object | None) -> str | None:
    if catalog_playlist_id is ...:
        return current.catalog_playlist_id
    if catalog_playlist_id is None:
        return None
    text = str(catalog_playlist_id).strip()
    return text or None


def _opened_at(current: CrowdSession, next_open: bool) -> str | None:
    if next_open and current.opened_at is None:
        return _now()
    if next_open and not current.open:
        return _now()
    return current.opened_at


def _refuse_empty_open(next_open: bool, next_catalog: str | None, next_free: bool) -> None:
    if next_open and next_catalog is None and not next_free:
        raise SessionError(
            "nothing_to_request",
            "an open session needs a catalog playlist or free text",
        )


def set_session(
    conn: sqlite3.Connection,
    *,
    open: bool | None = None,
    catalog_playlist_id: str | object | None = ...,
    free_text_allowed: bool | None = None,
    cooldown_s: int | None = None,
) -> CrowdSession:
    """Write the single session row. Ellipsis means leave the current value."""
    current = get_session(conn)
    next_open = current.open if open is None else bool(open)
    next_catalog = _catalog_value(current, catalog_playlist_id)
    next_free = current.free_text_allowed if free_text_allowed is None else bool(free_text_allowed)
    next_cooldown = current.cooldown_s if cooldown_s is None else int(cooldown_s)
    if next_cooldown < 1:
        raise SessionError("invalid_cooldown", "cooldown_s must be a positive integer")
    _refuse_empty_open(next_open, next_catalog, next_free)
    next_opened = _opened_at(current, next_open)
    conn.execute(
        """
        INSERT INTO crowd_request_session (
            id, open, catalog_playlist_id, free_text_allowed, cooldown_s, opened_at
        ) VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            open = excluded.open,
            catalog_playlist_id = excluded.catalog_playlist_id,
            free_text_allowed = excluded.free_text_allowed,
            cooldown_s = excluded.cooldown_s,
            opened_at = excluded.opened_at
        """,
        (1 if next_open else 0, next_catalog, 1 if next_free else 0, next_cooldown, next_opened),
    )
    return get_session(conn)


def _normalize_name(requester_name: str | None) -> str:
    name = (requester_name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise RequestError(
            "invalid_name",
            f"requester_name must be 1 to {NAME_MAX} characters",
        )
    return name


def _normalize_key(requester_key: str | None) -> str:
    key = (requester_key or "").strip()
    if not key:
        raise RequestError("invalid_key", "requester_key is required")
    return key


def add_request(
    conn: sqlite3.Connection,
    *,
    requester_name: str,
    requester_key: str,
    stable_id: str | None = None,
    text: str | None = None,
) -> CrowdRequest:
    name = _normalize_name(requester_name)
    key = _normalize_key(requester_key)
    track = (stable_id or "").strip() or None
    message = (text or "").strip() or None
    if (track is None) == (message is None):
        raise RequestError(
            "invalid_request",
            "provide exactly one of stable_id or text",
        )
    if message is not None and len(message) > TEXT_MAX:
        raise RequestError("invalid_text", f"text must be at most {TEXT_MAX} characters")
    kind: Kind = "track" if track is not None else "text"
    stamp = _now()
    request_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO crowd_requests (
            id, created_at, kind, stable_id, text, requester_name, requester_key,
            votes, status, status_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'new', ?)
        """,
        (request_id, stamp, kind, track, message, name, key, stamp),
    )
    row = get_request(conn, request_id)
    assert row is not None
    return row


def get_request(conn: sqlite3.Connection, request_id: str) -> CrowdRequest | None:
    row = conn.execute(
        """
        SELECT id, created_at, kind, stable_id, text, requester_name, requester_key,
               votes, status, status_at
        FROM crowd_requests WHERE id = ?
        """,
        (request_id,),
    ).fetchone()
    return None if row is None else _request_from_row(row)


def vote(conn: sqlite3.Connection, request_id: str) -> CrowdRequest | None:
    found = get_request(conn, request_id)
    if found is None:
        return None
    conn.execute("UPDATE crowd_requests SET votes = votes + 1 WHERE id = ?", (request_id,))
    return get_request(conn, request_id)


def set_status(conn: sqlite3.Connection, request_id: str, status: str) -> CrowdRequest | None:
    if status not in STATUSES:
        raise RequestError("invalid_status", f"status must be one of {', '.join(STATUSES)}")
    found = get_request(conn, request_id)
    if found is None:
        return None
    conn.execute(
        "UPDATE crowd_requests SET status = ?, status_at = ? WHERE id = ?",
        (status, _now(), request_id),
    )
    return get_request(conn, request_id)


def list_requests(conn: sqlite3.Connection, status: str = "new") -> list[CrowdRequest]:
    if status not in (*STATUSES, "all"):
        raise RequestError("invalid_status", "status must be new, loaded, dismissed or all")
    sql = """
        SELECT id, created_at, kind, stable_id, text, requester_name, requester_key,
               votes, status, status_at
        FROM crowd_requests
        """
    params: tuple[str, ...] = ()
    if status != "all":
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY created_at DESC, rowid DESC"
    return [_request_from_row(row) for row in conn.execute(sql, params)]


def clear_requests(conn: sqlite3.Connection) -> int:
    count = conn.execute("SELECT COUNT(*) FROM crowd_requests").fetchone()[0]
    conn.execute("DELETE FROM crowd_requests")
    return int(count)


__all__ = [
    "ADDED_TOPIC",
    "DEFAULT_COOLDOWN_S",
    "CrowdRequest",
    "CrowdSession",
    "RequestError",
    "SessionError",
    "VoteLedger",
    "add_request",
    "clear_requests",
    "ensure_schema",
    "get_request",
    "get_session",
    "list_requests",
    "load_denylist",
    "requester_fingerprint",
    "set_session",
    "set_status",
    "text_is_filtered",
    "vote",
]
