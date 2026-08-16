"""SMART-02 regression: DjayPlaylistWriter must refuse live writes when no
LiveWriteSession wraps the call AND djay (or Rekordbox) is running.

Tracked as forensics finding "P1-B" in CHANGELOG.md -- that is an audit
label, not a requirement ID; the requirement it defends is SMART-02 (the
materialization engine pushing a rule's result into djay Pro).

Forensics audit (PR #105) flagged that apps/smartlists/djay_writer.py
opens djay's MediaLibrary.db with ``mode=rwc`` inside ``_apply_op``,
relying entirely on callers (SafePlaylistWriter + safe_writer_session)
to have run the six-rail safety harness first. A future direct caller
could silently bypass rail 2 (the djay/Rekordbox pgrep gate).

This test asserts the self-guard: without ``safety_session=`` set, if
``pgrep`` reports djay or Rekordbox running, ``create_playlist`` raises
``PlaylistApplyError`` before any sqlite connection is opened.

We never launch djay or Rekordbox; we monkeypatch
``apps.sync.safety._is_running`` to simulate a live process. The
fixture DB is a throwaway sqlite file in ``tmp_path`` — the guard short-
circuits before any DB interaction so no schema is required.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.smartlists.djay_writer import DjayPlaylistWriter
from apps.sync import safety as _safety
from apps.sync.playlist_apply import PlaylistApplyError


@pytest.fixture
def _tmp_djay_db(tmp_path: Path) -> Path:
    db = tmp_path / "MediaLibrary.db"
    con = sqlite3.connect(str(db))
    con.close()
    return db


@pytest.fixture
def _tmp_state_conn() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE track_vendor_ids "
        "(stable_id TEXT, vendor TEXT, vendor_id TEXT);"
    )
    return con


@pytest.mark.requirement("SMART-02")
def test_djay_writer_refuses_without_safety_session(
    _tmp_djay_db: Path,
    _tmp_state_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_playlist + simulated running djay + no session => refuse."""
    monkeypatch.setattr(
        _safety, "_is_running", lambda name: name == "djay Pro"
    )
    w = DjayPlaylistWriter(
        djay_db_path=_tmp_djay_db, state_conn=_tmp_state_conn
    )
    assert w.safety_session is None
    with pytest.raises(PlaylistApplyError, match="self-guard"):
        w.create_playlist("[SL] Blocked", ["sid-1", "sid-2"])


@pytest.mark.requirement("SMART-02")
def test_djay_writer_refuses_when_rekordbox_running(
    _tmp_djay_db: Path,
    _tmp_state_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard also trips when Rekordbox is alive (paranoia rail)."""
    monkeypatch.setattr(
        _safety, "_is_running", lambda name: name == "Rekordbox"
    )
    w = DjayPlaylistWriter(
        djay_db_path=_tmp_djay_db, state_conn=_tmp_state_conn
    )
    with pytest.raises(PlaylistApplyError, match="self-guard"):
        w.create_playlist("[SL] Blocked", ["sid-1", "sid-2"])


@pytest.mark.requirement("SMART-02")
def test_djay_writer_bypasses_guard_when_safety_session_set(
    _tmp_djay_db: Path,
    _tmp_state_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """safety_session= sentinel => inline guard is skipped."""
    monkeypatch.setattr(_safety, "_is_running", lambda _n: True)
    sentinel = object()  # stands in for an active LiveWriteSession
    w = DjayPlaylistWriter(
        djay_db_path=_tmp_djay_db,
        state_conn=_tmp_state_conn,
        safety_session=sentinel,
    )
    # Guard must not fire. The downstream write will likely fail for
    # other reasons (no schema, no mapping) — that's fine; we only
    # assert the guard's PlaylistApplyError is absent.
    try:
        w.create_playlist("[SL] Allowed", ["sid-1"])
    except PlaylistApplyError as exc:
        assert "self-guard" not in str(exc), (
            f"guard should have been skipped, got: {exc}"
        )
    except Exception:
        # Any other failure (missing mapping, missing schema) is OK —
        # confirms we passed the guard.
        pass


@pytest.mark.requirement("SMART-02")
def test_djay_writer_guard_is_noop_when_processes_quit(
    _tmp_djay_db: Path,
    _tmp_state_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal path: no DJ app running => guard silently passes."""
    monkeypatch.setattr(_safety, "_is_running", lambda _n: False)
    w = DjayPlaylistWriter(
        djay_db_path=_tmp_djay_db, state_conn=_tmp_state_conn
    )
    # Explicit call: must not raise.
    w._assert_safe_to_write()
