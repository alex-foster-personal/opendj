"""Hub push when the hub sqlite database cannot grow.

[if] the hub sqlite cannot take a push write [then] the spoke gets 507 SYNC_HUB_STORAGE and the fence stays put, [else stop].
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, generation, hub_wal_keeper, protocol, service_storage
from apps.sync_hub import service as sync_service
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _T2,
    _insert_track,
    _open,
    _sync,
)

pytestmark = pytest.mark.requirement("CAT-04")

_TRACK_ONE = "storage-full-track-one"
_TRACK_TWO = "storage-full-track-two"
_TRACK_GEN = "storage-gen-anchor-track"
_SPOKE_NAME = "storage-spoke"


def _spoke_push_seq(spoke_dir: Path, peer: str) -> int:
    conn = state_db.open_rw(client.state_db_path(spoke_dir))
    try:
        return engine.read_watermark(conn, peer).last_push_seq
    finally:
        conn.close()


def _hub_changelog_max_seq(hub_dir: Path) -> int:
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM hub_changelog").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _hub_changelog_count(hub_dir: Path) -> int:
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        row = conn.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _hub_changelog_has_track(hub_dir: Path, stable_id: str) -> bool:
    encoded = protocol.encode_row_pk((stable_id,))
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        row = conn.execute(
            "SELECT 1 FROM hub_changelog WHERE table_name = 'tracks' AND row_pk = ? LIMIT 1",
            (encoded,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _track_title_on_hub(hub_dir: Path, stable_id: str) -> str | None:
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        row = conn.execute(
            "SELECT title FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def _cap_hub_pages(
    monkeypatch: pytest.MonkeyPatch, hub_dir: Path, hub: TestClientTransport
) -> int:
    hub_db_path = client.state_db_path(hub_dir).resolve()
    real_open_rw = state_db.open_rw
    # Leaving WAL needs the database to itself; the running hub's keeper
    # connection would answer "database is locked" (LIBM-120 L6 round 3).
    assert hub_wal_keeper.close_wal_keepers(hub.app_state) == 1
    prep = real_open_rw(hub_db_path)
    capped = False
    try:
        prep.execute("VACUUM")
        prep.execute("PRAGMA journal_mode = DELETE")
        page_count = int(prep.execute("PRAGMA page_count").fetchone()[0])
        prep.execute(f"PRAGMA max_page_count = {page_count}")
        for index in range(50000):
            try:
                prep.execute("BEGIN")
                prep.execute(
                    """
                    INSERT INTO tracks(
                        stable_id, stable_id_tier, title, created_at, updated_at,
                        origin_device_id, deleted_at
                    )
                    VALUES (?, 'inferred', ?, ?, ?, ?, NULL)
                    """,
                    (f"cap-probe-{index:06d}", f"probe {index}", _T0, _T0, _DEV_A),
                )
                prep.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                try:
                    prep.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if service_storage.is_hub_storage_full(exc):
                    capped = True
                    break
                raise
        if not capped:
            pytest.fail("hub sqlite cap did not produce SQLITE_FULL after filling")
        page_count = int(prep.execute("PRAGMA page_count").fetchone()[0])
    finally:
        prep.close()

    def wrapper(
        path: Path | None = None,
        *,
        apply_schema: bool = True,
        check_same_thread: bool = True,
    ) -> sqlite3.Connection:
        conn = real_open_rw(
            path,
            apply_schema=apply_schema,
            check_same_thread=check_same_thread,
        )
        if path is not None and Path(path).resolve() == hub_db_path:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute(f"PRAGMA max_page_count = {page_count}")
        return conn

    monkeypatch.setattr(sync_service.state_db, "open_rw", wrapper)
    return page_count


def _block_generation_anchor_writes(hub_dir: Path) -> int:
    """Remove write permission on the hub data dir root, not ``state/``."""
    saved_mode = hub_dir.stat().st_mode
    os.chmod(
        hub_dir,
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
    )
    return saved_mode


def _restore_generation_anchor_writes(hub_dir: Path, saved_mode: int) -> None:
    os.chmod(hub_dir, saved_mode)


def _lift_hub_pages(monkeypatch: pytest.MonkeyPatch, hub_dir: Path) -> None:
    monkeypatch.undo()
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM tracks WHERE stable_id LIKE 'cap-probe-%'")
        conn.execute("COMMIT")
        conn.execute("VACUUM")
    finally:
        conn.close()


def _first_successful_sync(
    enroll_spoke_dir: Path,
    enroll_hub: TestClientTransport,
) -> client.SyncResult:
    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(
            conn,
            _TRACK_ONE,
            title="first track",
            updated_at=_T1,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()
    return _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)


def test_push_to_a_full_hub_returns_sync_hub_storage(
    monkeypatch: pytest.MonkeyPatch,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """[if] the hub sqlite cannot grow [then] push is 507 SYNC_HUB_STORAGE, [else stop]."""
    _first_successful_sync(enroll_spoke_dir, enroll_hub)
    _cap_hub_pages(monkeypatch, enroll_hub_dir, enroll_hub)

    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(
            conn,
            _TRACK_TWO,
            title="second track",
            updated_at=_T2,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(client.SyncTransportError) as excinfo:
        _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)

    message = str(excinfo.value)
    assert "HTTP 507" in message
    assert "SYNC_HUB_STORAGE" in message
    assert "Internal Server Error" not in message


def test_failed_full_hub_push_leaves_fence_and_changelog_unchanged_then_converges(
    monkeypatch: pytest.MonkeyPatch,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """[if] that push fails [then] fence and changelog stay put and later sync converges, [else stop]."""
    first = _first_successful_sync(enroll_spoke_dir, enroll_hub)
    peer = first.hub_machine_id
    push_seq_before = _spoke_push_seq(enroll_spoke_dir, peer)
    max_seq_before = _hub_changelog_max_seq(enroll_hub_dir)
    count_before = _hub_changelog_count(enroll_hub_dir)

    _cap_hub_pages(monkeypatch, enroll_hub_dir, enroll_hub)

    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(
            conn,
            _TRACK_TWO,
            title="held back track",
            updated_at=_T2,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(client.SyncTransportError) as excinfo:
        _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)
    message = str(excinfo.value)
    assert "HTTP 507" in message
    assert "SYNC_HUB_STORAGE" in message

    assert _spoke_push_seq(enroll_spoke_dir, peer) == push_seq_before
    assert _hub_changelog_max_seq(enroll_hub_dir) == max_seq_before
    assert _hub_changelog_count(enroll_hub_dir) == count_before
    assert not _hub_changelog_has_track(enroll_hub_dir, _TRACK_TWO)

    _lift_hub_pages(monkeypatch, enroll_hub_dir)

    result = _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)
    assert re.fullmatch(r"[0-9a-f]{64}", result.digest)
    assert _track_title_on_hub(enroll_hub_dir, _TRACK_TWO) == "held back track"


def test_generation_anchor_failure_after_commit_is_documented(
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """[if] generation observe fails after COMMIT [then] rows land on the hub but the spoke fence does not, [else stop]."""
    first = _first_successful_sync(enroll_spoke_dir, enroll_hub)
    peer = first.hub_machine_id
    push_seq_before = _spoke_push_seq(enroll_spoke_dir, peer)
    max_seq_before = _hub_changelog_max_seq(enroll_hub_dir)
    count_before = _hub_changelog_count(enroll_hub_dir)

    anchor = generation.anchor_path(enroll_hub_dir)
    assert anchor.is_file()
    saved_hub_mode = _block_generation_anchor_writes(enroll_hub_dir)

    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(
            conn,
            _TRACK_GEN,
            title="generation failure track",
            updated_at=_T2,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(client.SyncTransportError) as excinfo:
        _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)
    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert "SYNC_HUB_GENERATION" in message
    assert "HTTP 507" not in message
    assert "SYNC_HUB_STORAGE" not in message

    assert _spoke_push_seq(enroll_spoke_dir, peer) == push_seq_before
    assert _hub_changelog_max_seq(enroll_hub_dir) > max_seq_before
    assert _hub_changelog_count(enroll_hub_dir) > count_before
    assert _hub_changelog_has_track(enroll_hub_dir, _TRACK_GEN)

    _restore_generation_anchor_writes(enroll_hub_dir, saved_hub_mode)

    result = _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)
    assert re.fullmatch(r"[0-9a-f]{64}", result.digest)


def test_mapper_maps_sqlite_full_to_sync_hub_storage() -> None:
    """[if] sqlite reports disk full [then] the mapper answers 507 SYNC_HUB_STORAGE, [else stop]."""
    exc = sqlite3.OperationalError("database or disk is full")
    try:
        exc.sqlite_errorcode = sqlite3.SQLITE_FULL  # type: ignore[attr-defined]
    except AttributeError:
        pass
    http_exc = service_storage.http_exception_for(exc)
    assert http_exc.status_code == 507
    assert http_exc.detail == {
        "code": "SYNC_HUB_STORAGE",
        "message": "database or disk is full",
    }


def test_mapper_does_not_claim_lock_errors_as_storage_full() -> None:
    """[if] sqlite reports a lock error [then] the mapper does not call it storage full, [else stop]."""
    exc = sqlite3.OperationalError("database is locked")
    assert not service_storage.is_hub_storage_full(exc)
