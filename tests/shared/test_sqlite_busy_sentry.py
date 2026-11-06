"""OBS-5380: SQLite lock contention stays in the local sink, not Sentry.

Sentry OPEN-DJ-BE-7A titled ``OperationalError: database is locked``. STATE-14
already maps that to HTTP 503; the SDK still captured the exception.

[if] Sentry sees a locked OperationalError [then] it is dropped locally, [else stop].
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.state.db import StateStoreBusyError
from apps.shared.telemetry.sink import ErrorEvent, maybe_send_sentry
from apps.shared.telemetry.sqlite_busy import (
    exception_from_sentry_hint,
    is_local_only_sqlite_busy,
)

pytestmark = pytest.mark.requirement("OBS-5380")


def test_locked_operational_error_is_local_only() -> None:
    locked = sqlite3.OperationalError("database is locked")
    assert is_local_only_sqlite_busy(exc=locked) is True
    assert is_local_only_sqlite_busy(exc=StateStoreBusyError("database is locked")) is True


def test_other_operational_errors_are_not_local_only() -> None:
    missing = sqlite3.OperationalError("no such table: analysis")
    assert is_local_only_sqlite_busy(exc=missing) is False
    assert is_local_only_sqlite_busy(exc=ValueError("database is locked")) is False


def test_sentry_hint_and_event_payloads_classify_the_same_way() -> None:
    locked = sqlite3.OperationalError("database is locked")
    hint = {"exc_info": (sqlite3.OperationalError, locked, None)}
    assert exception_from_sentry_hint(hint) is locked
    event = {
        "exception": {
            "values": [{"type": "OperationalError", "value": "database is locked"}]
        }
    }
    assert is_local_only_sqlite_busy(event=event) is True
    event_ok = {
        "exception": {
            "values": [{"type": "OperationalError", "value": "no such table: foo"}]
        }
    }
    assert is_local_only_sqlite_busy(event=event_ok) is False
    log_message = (
        "ahead analysis tick failed\n"
        "sqlite3.OperationalError: database is locked"
    )
    assert is_local_only_sqlite_busy(message=log_message) is True
    assert is_local_only_sqlite_busy(message="file is locked") is False


def test_maybe_send_sentry_drops_busy_log_text() -> None:
    """[if] warning_log forwards a locked OperationalError [then] Sentry is skipped, [else stop]."""
    event = ErrorEvent(
        error_id="eid-test",
        host="test-host",
        build_sha="deadbeef",
        kind="engine",
        source_site="engine:ahead_analysis",
        message=(
            "ahead analysis tick failed\n"
            "sqlite3.OperationalError: database is locked"
        ),
        timestamp="2026-10-05T00:00:00.000+00:00",
    )
    assert maybe_send_sentry(event) is None
