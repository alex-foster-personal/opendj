"""Expected SQLite writer-lock contention stays local (OBS-5380).

STATE-14 already maps lock exhaustion to HTTP 503. Sentry's FastAPI/Starlette
integrations still capture the underlying ``OperationalError`` before that
handler returns, which is how OPEN-DJ-BE-7A opened as
``OperationalError: database is locked``. The local JSONL sink keeps the row;
Sentry does not.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.shared.state.db import StateStoreBusyError, is_sqlite_busy


def is_sqlite_busy_text(message: str) -> bool:
    """True for SQLite's lock-exhaustion phrasing, never a generic 'locked'."""
    lower = message.lower()
    return "database is locked" in lower or "database is busy" in lower


def exception_from_sentry_hint(hint: Mapping[str, Any] | None) -> BaseException | None:
    """The captured exception Sentry put on ``hint``, if any."""
    if hint is None:
        return None
    exc_info = hint.get("exc_info")
    if isinstance(exc_info, tuple) and len(exc_info) >= 2:
        value = exc_info[1]
        if isinstance(value, BaseException):
            return value
    value = hint.get("exc_value")
    return value if isinstance(value, BaseException) else None


def _event_carries_sqlite_busy(event: Mapping[str, Any]) -> bool:
    values = event.get("exception")
    if isinstance(values, Mapping):
        items = values.get("values")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                typ = str(item.get("type") or "")
                val = str(item.get("value") or "")
                if "StateStoreBusyError" in typ:
                    return True
                if "OperationalError" in typ and is_sqlite_busy_text(val):
                    return True
    for raw in (event.get("message"),):
        if isinstance(raw, str) and _busy_log_text(raw):
            return True
    logentry = event.get("logentry")
    if isinstance(logentry, Mapping):
        for key in ("formatted", "message"):
            formatted = logentry.get(key)
            if isinstance(formatted, str) and _busy_log_text(formatted):
                return True
    return False


def _busy_log_text(message: str) -> bool:
    if not is_sqlite_busy_text(message):
        return False
    lower = message.lower()
    return (
        "operationalerror" in lower
        or "statestorebusyerror" in lower
        or "state store is busy" in lower
    )


def is_local_only_sqlite_busy(
    *,
    exc: BaseException | None = None,
    event: Mapping[str, Any] | None = None,
    message: str | None = None,
) -> bool:
    """True when this event is SQLite lock contention, not an application bug."""
    if exc is not None and (
        isinstance(exc, StateStoreBusyError) or is_sqlite_busy(exc)
    ):
        return True
    if event is not None and _event_carries_sqlite_busy(event):
        return True
    return bool(message) and _busy_log_text(message)
