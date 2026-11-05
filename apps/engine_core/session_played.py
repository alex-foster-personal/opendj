"""Session played set (SESSION-PLAYED-01): which tracks this DJ session already played.

The library paints these rows green. The set lives on disk, written atomically
(temp + fsync + rename, ``apps.rescue.http.write_atomic``), so a crash, force
quit or relaunch never loses or corrupts it.

Expiry is explicit and never destructive: a session counts as the same session
for ``SESSION_IDLE_EXPIRY_MS`` (12 h) after its last activity. Past that the
document reads ``expired: true`` with every id still in it, and the UI asks
"Restore last session from <weekday time>?". Only ``POST /session/new`` ends a
session, and it archives the old file under ``state/session/archive/``.

Routes: GET/POST ``/api/v1/session/played``, POST ``/api/v1/session/resume``,
POST ``/api/v1/session/new``. CLI ``opendj session``; MCP ``session_played``,
``session_resume``, ``session_new``.

-Claude
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from apps.engine_core.config import EngineConfig
from apps.rescue.http import write_atomic

PLAYED_PATH = "/api/v1/session/played"
RESUME_PATH = "/api/v1/session/resume"
NEW_PATH = "/api/v1/session/new"
SESSION_IDLE_EXPIRY_MS = 12 * 60 * 60 * 1000
SCHEMA = 1

_LOCK = threading.Lock()


class SessionPlayedCorrupt(RuntimeError):
    """The on-disk file exists but does not parse; never papered over."""


# ----- store


class SessionPlayedStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "state" / "session" / "played.json"
        self._archive_dir = self.path.parent / "archive"

    def _read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionPlayedCorrupt(f"session played file {self.path} unreadable: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("played"), list):
            raise SessionPlayedCorrupt(f"session played file {self.path} has an unknown shape")
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        write_atomic(self.path, json.dumps(doc, indent=1) + "\n")

    @staticmethod
    def _fresh(now_ms: int) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "session_id": uuid.uuid4().hex,
            "started_at_ms": now_ms,
            "last_activity_ms": now_ms,
            "played": [],
        }

    @staticmethod
    def _status(doc: dict[str, Any] | None, now_ms: int) -> dict[str, Any]:
        if doc is None:
            return {
                "session_id": None,
                "started_at_ms": None,
                "last_activity_ms": None,
                "idle_ms": 0,
                "expired": False,
                "expiry_ms": SESSION_IDLE_EXPIRY_MS,
                "count": 0,
                "played_ids": [],
                "played": [],
            }
        idle_ms = max(0, now_ms - int(doc["last_activity_ms"]))
        played = [{"stable_id": str(row["stable_id"]), "at_ms": int(row["at_ms"])} for row in doc["played"]]
        return {
            "session_id": doc["session_id"],
            "started_at_ms": int(doc["started_at_ms"]),
            "last_activity_ms": int(doc["last_activity_ms"]),
            "idle_ms": idle_ms,
            "expired": idle_ms > SESSION_IDLE_EXPIRY_MS,
            "expiry_ms": SESSION_IDLE_EXPIRY_MS,
            "count": len(played),
            "played_ids": [row["stable_id"] for row in played],
            "played": played,
        }

    def status(self, *, now_ms: int) -> dict[str, Any]:
        with _LOCK:
            return self._status(self._read(), now_ms)

    def add(self, stable_id: str, *, now_ms: int) -> dict[str, Any]:
        """Mark one track played. Appends to the stored session even when it is expired:
        the UI holds adds until the DJ answers the restore prompt."""
        with _LOCK:
            doc = self._read() or self._fresh(now_ms)
            if all(row["stable_id"] != stable_id for row in doc["played"]):
                doc["played"].append({"stable_id": stable_id, "at_ms": now_ms})
            doc["last_activity_ms"] = max(int(doc["last_activity_ms"]), now_ms)
            self._write(doc)
            return self._status(doc, now_ms)

    def resume(self, *, now_ms: int) -> dict[str, Any] | None:
        with _LOCK:
            doc = self._read()
            if doc is None:
                return None
            doc["last_activity_ms"] = now_ms
            self._write(doc)
            return self._status(doc, now_ms)

    def new_session(self, *, now_ms: int) -> dict[str, Any]:
        with _LOCK:
            doc = self._read()
            if doc is not None:
                write_atomic(
                    self._archive_dir / f"played-{int(doc['started_at_ms'])}-{doc['session_id'][:8]}.json",
                    json.dumps(doc, indent=1) + "\n",
                )
            fresh = self._fresh(now_ms)
            self._write(fresh)
            return self._status(fresh, now_ms)


# ----- HTTP


class SessionPlayedRowOut(BaseModel):
    stable_id: str
    at_ms: int


class SessionPlayedOut(BaseModel):
    session_id: str | None
    started_at_ms: int | None
    last_activity_ms: int | None
    idle_ms: int
    expired: bool = Field(description="Idle longer than expiry_ms; ids are kept until resume or new")
    expiry_ms: int
    count: int
    played_ids: list[str]
    played: list[SessionPlayedRowOut]


class SessionPlayedIn(BaseModel):
    stable_id: str

    @field_validator("stable_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("stable_id must not be blank")
        return value.strip()


def _store(request: Request) -> SessionPlayedStore:
    cfg = getattr(request.app.state, "engine_cfg", None)
    if not isinstance(cfg, EngineConfig):
        raise HTTPException(status_code=503, detail="engine config unavailable")
    return SessionPlayedStore(cfg.data_dir)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _guard(fn: Any) -> Any:
    try:
        return fn()
    except SessionPlayedCorrupt as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def add_session_played_routes(app: FastAPI) -> None:
    @app.get(PLAYED_PATH, response_model=SessionPlayedOut, tags=["performance"], name="session_played_get")
    def get_session_played(request: Request) -> dict[str, Any]:
        store = _store(request)
        return _guard(lambda: store.status(now_ms=_now_ms()))

    @app.post(PLAYED_PATH, response_model=SessionPlayedOut, tags=["performance"], name="session_played_add")
    def add_session_played(body: SessionPlayedIn, request: Request) -> dict[str, Any]:
        store = _store(request)
        return _guard(lambda: store.add(body.stable_id, now_ms=_now_ms()))

    @app.post(RESUME_PATH, response_model=SessionPlayedOut, tags=["performance"], name="session_resume")
    def resume_session(request: Request) -> dict[str, Any]:
        store = _store(request)
        doc = _guard(lambda: store.resume(now_ms=_now_ms()))
        if doc is None:
            raise HTTPException(status_code=404, detail="no session to resume")
        return doc

    @app.post(NEW_PATH, response_model=SessionPlayedOut, tags=["performance"], name="session_new")
    def new_session(request: Request) -> dict[str, Any]:
        store = _store(request)
        return _guard(lambda: store.new_session(now_ms=_now_ms()))


__all__ = [
    "NEW_PATH",
    "PLAYED_PATH",
    "RESUME_PATH",
    "SESSION_IDLE_EXPIRY_MS",
    "SessionPlayedStore",
    "add_session_played_routes",
]
