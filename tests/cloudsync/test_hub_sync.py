"""Bidirectional hub sync: convergence, LWW, tombstones, digest.

Contract under test: ``specs/design_decision_04.md`` over the v6 schema of
``specs/design_decision_05.md``. One hub data dir plus two spoke data dirs,
all three real migrated DBs; the hub is the FastAPI router behind a
``TestClient``, so nothing here touches a network or a mock.

Acceptance criteria, one test each:
- if an edit on one spoke does not reach the other after two syncs, the hub
  is not converging -- broken.
- if the later ``updated_at`` does not win, or a tie is not broken by the
  greater ``origin_device_id``, the conflict rule is not LWW -- broken.
- if a ``deleted_at`` stamp does not propagate, deletes are not soft and the
  row comes back from the dead on the next sync -- broken.
- if a playlist edit merges membership row-by-row instead of replacing the
  peer's whole list, an ordered list interleaves into garbage -- broken.
- if a second immediate sync accepts rows or moves the hub seq, the
  watermark is not holding and every sync rewrites the whole library --
  broken.
- if a spoke that edits a row WITHOUT stamping ``updated_at`` still reports a
  clean sync, the digest is not verifying anything -- broken.
- if a legacy row with NULL ``updated_at`` beats a real edit, migration-era
  rows overwrite live data on first sync -- broken.

Since ADR 08 the helpers below write the way a real writer does: canonical
timestamps, and every synced-table write appended to ``local_changelog``
through :func:`apps.shared.state.sync_stamp.stamp_and_log`. That is not
ceremony -- the push fence reads that changelog, so a fixture that skipped it
would test a spoke whose edits are invisible to its own push.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import client, engine, protocol, service
from tests.cloudsync.enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("CAT-04")

# Canonical stamps (apps.shared.state.sync_stamp.CANONICAL_FORMAT): UTC,
# fixed-width microseconds, explicit offset. Anything else is re-emitted in
# this form at the protocol boundary, so writing the fixtures in it keeps the
# stored value and the wire value identical and the assertions literal.
_T0 = "2026-08-30T09:00:00.000000+00:00"
_T1 = "2026-08-30T10:00:00.000000+00:00"
_T2 = "2026-08-30T11:00:00.000000+00:00"
_T3 = "2026-08-30T12:00:00.000000+00:00"

_DEV_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_DEV_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# ----- transport -----------------------------------------------------------


# The class lives in tests/cloudsync/enrollment_transport.py so `conftest`
# can build a hub fixture without importing this test module. Re-exported
# under its original private name: every existing importer keeps working,
# and there is still exactly ONE implementation of it rather than two that
# agree today.
_TestClientTransport = TestClientTransport


# ----- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    """The hub's DATA DIR, which does not exist yet.

    Guarantees it is empty and private to this test: no state DB, no
    ``machine-id`` file, no ``machines`` row. The first ``_open`` or the
    ``hub`` fixture below creates them, so a test observes the bootstrap
    rather than inheriting one.
    """
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    """Spoke A's empty data dir. Same guarantees as :func:`hub_dir`.

    Its machine id is minted from this path on first open, so A and B are
    distinct machines with distinct ``origin_device_id`` values without any
    test saying so. ``_DEV_A`` / ``_DEV_B`` are the ids fixtures WRITE onto
    rows; they are deliberately not these machines' ids.
    """
    return tmp_path / "spoke-a"


@pytest.fixture
def spoke_b(tmp_path: Path) -> Path:
    """Spoke B's empty data dir. Same guarantees as :func:`spoke_a`."""
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    """The real sync router over the real ASGI stack, on an EMPTY hub DB.

    Guarantees: the hub holds no synced rows and no changelog entries until a
    spoke pushes, its machine name is ``hub``, and every call a test makes
    goes through the same routing, validation and error handling a network
    client would hit. Nothing here is a stub of the hub -- only the socket is
    absent.
    """
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


# ----- helpers -------------------------------------------------------------


def _open(data_dir: Path) -> sqlite3.Connection:
    """A migrated state DB for ``data_dir`` (created on first call)."""
    return state_db.open_rw(client.state_db_path(data_dir))


def _sync(
    data_dir: Path, hub: _TestClientTransport, name: str
) -> client.SyncResult:
    return client.run_sync(data_dir, "http://hub.invalid", transport=hub, name=name)


def _log(
    conn: sqlite3.Connection,
    table: str,
    row_pk: tuple[Any, ...],
    origin: str,
    updated_at: str,
) -> str:
    """Stamp one synced-table write and append it to ``local_changelog``.

    What every in-repo writer now does (ADR 08 points 2 and 3). Returns the
    canonical stamp the row must store.
    """
    return sync_stamp.stamp_and_log(
        conn, table, row_pk, origin, now=updated_at
    ).updated_at


def _insert_track(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    title: str,
    updated_at: str,
    origin: str,
    deleted_at: str | None = None,
) -> None:
    stamped = _log(conn, "tracks", (stable_id,), origin, updated_at)
    conn.execute(
        """
        INSERT INTO tracks(
            stable_id, stable_id_tier, title, created_at, updated_at,
            origin_device_id, deleted_at
        )
        VALUES (?, 'inferred', ?, ?, ?, ?, ?)
        """,
        (stable_id, title, _T0, stamped, origin, deleted_at),
    )


def _set_track_title(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    title: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "tracks", (stable_id,), origin, updated_at)
    conn.execute(
        "UPDATE tracks SET title = ?, updated_at = ?, origin_device_id = ? "
        "WHERE stable_id = ?",
        (title, stamped, origin, stable_id),
    )


def _soft_delete_track(
    conn: sqlite3.Connection, stable_id: str, *, deleted_at: str, origin: str
) -> None:
    stamped = _log(conn, "tracks", (stable_id,), origin, deleted_at)
    conn.execute(
        "UPDATE tracks SET deleted_at = ?, updated_at = ?, "
        "origin_device_id = ? WHERE stable_id = ?",
        (stamped, stamped, origin, stable_id),
    )


def _track_title(conn: sqlite3.Connection, stable_id: str) -> str | None:
    row = conn.execute(
        "SELECT title FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    return None if row is None else row[0]


def _insert_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    name: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "playlists", (playlist_id,), origin, updated_at)
    conn.execute(
        """
        INSERT INTO playlists(
            playlist_id, name, vendor, vendor_pl_id, created_at, updated_at,
            origin_device_id
        )
        VALUES (?, ?, 'open-dj', ?, ?, ?, ?)
        """,
        (playlist_id, name, playlist_id, _T0, stamped, origin),
    )


def _set_members(
    conn: sqlite3.Connection,
    playlist_id: str,
    stable_ids: tuple[str, ...],
    *,
    updated_at: str,
    origin: str,
) -> None:
    """Replace membership locally AND stamp the playlist, as a writer must."""
    stamped = _log(conn, "playlists", (playlist_id,), origin, updated_at)
    conn.execute(
        "DELETE FROM playlist_memberships WHERE playlist_id = ?", (playlist_id,)
    )
    for position, stable_id in enumerate(stable_ids):
        conn.execute(
            """
            INSERT INTO playlist_memberships(
                playlist_id, stable_id, position, updated_at, origin_device_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (playlist_id, stable_id, position, stamped, origin),
        )
    conn.execute(
        "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
        "WHERE playlist_id = ?",
        (stamped, origin, playlist_id),
    )


def _members(conn: sqlite3.Connection, playlist_id: str) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? "
            "ORDER BY position",
            (playlist_id,),
        )
    )


def _seed_common_track(data_dirs: tuple[Path, ...], stable_id: str) -> None:
    """The same starting row on several spokes, so nothing conflicts."""
    for data_dir in data_dirs:
        conn = _open(data_dir)
        try:
            _insert_track(
                conn, stable_id, title="original", updated_at=_T0, origin=_DEV_A
            )
        finally:
            conn.close()


# ----- tests ---------------------------------------------------------------


def test_edit_converges_across_two_spokes(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """A's edit reaches B through the hub."""
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    conn_a = _open(spoke_a)
    try:
        _set_track_title(
            conn_a, "trk-1", title="edited on A", updated_at=_T1, origin=_DEV_A
        )
    finally:
        conn_a.close()

    result_a = _sync(spoke_a, hub, "spoke-a")
    result_b = _sync(spoke_b, hub, "spoke-b")

    assert result_a.accepted == 1
    assert result_b.applied == 1

    conn_b = _open(spoke_b)
    try:
        assert _track_title(conn_b, "trk-1") == "edited on A"
    finally:
        conn_b.close()


def test_later_updated_at_wins_and_ties_break_on_device_id(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """LWW on ``(updated_at, origin_device_id)``, lexicographic tiebreak."""
    _seed_common_track((spoke_a, spoke_b), "trk-late")
    _seed_common_track((spoke_a, spoke_b), "trk-tie")

    conn_a = _open(spoke_a)
    try:
        # A edits later in wall-clock terms; A must win regardless of order.
        _set_track_title(
            conn_a, "trk-late", title="A at T2", updated_at=_T2, origin=_DEV_A
        )
        # Identical stamps: the greater device id must win, and _DEV_B > _DEV_A.
        _set_track_title(
            conn_a, "trk-tie", title="A at T1", updated_at=_T1, origin=_DEV_A
        )
    finally:
        conn_a.close()

    conn_b = _open(spoke_b)
    try:
        _set_track_title(
            conn_b, "trk-late", title="B at T1", updated_at=_T1, origin=_DEV_B
        )
        _set_track_title(
            conn_b, "trk-tie", title="B at T1", updated_at=_T1, origin=_DEV_B
        )
    finally:
        conn_b.close()

    # A pushes first, so B's rows arrive against an already-populated hub.
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")

    for data_dir in (spoke_a, spoke_b):
        conn = _open(data_dir)
        try:
            assert _track_title(conn, "trk-late") == "A at T2", data_dir.name
            assert _track_title(conn, "trk-tie") == "B at T1", data_dir.name
        finally:
            conn.close()


def test_tombstone_propagates(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """A soft delete on A becomes a soft delete on B (ADR 04 c4)."""
    _seed_common_track((spoke_a, spoke_b), "trk-dead")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_a = _open(spoke_a)
    try:
        _soft_delete_track(conn_a, "trk-dead", deleted_at=_T2, origin=_DEV_A)
    finally:
        conn_a.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_b = _open(spoke_b)
    try:
        row = conn_b.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?", ("trk-dead",)
        ).fetchone()
        assert row is not None, "tombstone hard-deleted the row instead"
        assert row[0] == _T2
    finally:
        conn_b.close()


def test_playlist_membership_is_replaced_wholesale(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """A membership edit replaces the peer's list, never merges into it."""
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    _seed_common_track((spoke_a, spoke_b), "trk-2")

    conn_a = _open(spoke_a)
    try:
        _insert_playlist(
            conn_a, "pl-1", name="OLTF", updated_at=_T1, origin=_DEV_A
        )
        _set_members(
            conn_a, "pl-1", ("trk-1", "trk-2"), updated_at=_T1, origin=_DEV_A
        )
    finally:
        conn_a.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_b = _open(spoke_b)
    try:
        assert _members(conn_b, "pl-1") == ("trk-1", "trk-2")
    finally:
        conn_b.close()

    # A drops trk-1, adds a brand new trk-3, and reorders.
    conn_a = _open(spoke_a)
    try:
        _insert_track(
            conn_a, "trk-3", title="new on A", updated_at=_T2, origin=_DEV_A
        )
        _set_members(
            conn_a, "pl-1", ("trk-3", "trk-2"), updated_at=_T2, origin=_DEV_A
        )
    finally:
        conn_a.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_b = _open(spoke_b)
    try:
        assert _members(conn_b, "pl-1") == ("trk-3", "trk-2")
    finally:
        conn_b.close()


def test_resync_is_a_no_op(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """The watermark holds: an immediate second sync changes nothing."""
    _seed_common_track((spoke_a,), "trk-1")
    first = _sync(spoke_a, hub, "spoke-a")
    assert first.accepted == 1

    hub_conn = _open(hub_dir)
    try:
        seq_after_first = engine.current_seq(hub_conn)
    finally:
        hub_conn.close()

    second = _sync(spoke_a, hub, "spoke-a")

    assert second.accepted == 0, "hub re-accepted an unchanged row"
    assert second.applied == 0, "spoke re-applied its own row"

    hub_conn = _open(hub_dir)
    try:
        assert engine.current_seq(hub_conn) == seq_after_first
    finally:
        hub_conn.close()


def test_digest_mismatch_raises(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """An edit that forgets to stamp ``updated_at`` is caught, not merged."""
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    conn_a = _open(spoke_a)
    try:
        # Exactly the bug the digest exists for: content changed, sync
        # columns untouched, so LWW has nothing to notice.
        conn_a.execute(
            "UPDATE tracks SET title = ? WHERE stable_id = ?",
            ("silently rewritten", "trk-1"),
        )
    finally:
        conn_a.close()

    with pytest.raises(client.SyncDigestMismatch) as excinfo:
        _sync(spoke_a, hub, "spoke-a")
    assert "tracks" in str(excinfo.value)


def test_legacy_null_updated_at_loses_to_a_real_edit(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """A pre-v6 row (NULL ``updated_at``) syncs as epoch-old (ADR 04 c7)."""
    _seed_common_track((spoke_a, spoke_b), "trk-1")

    conn_b = _open(spoke_b)
    try:
        # B holds the legacy row: no updated_at, no origin.
        conn_b.execute(
            """
            INSERT INTO track_fields(
                stable_id, field_name, value_json, source, modified_at,
                updated_at, origin_device_id
            )
            VALUES ('trk-1', 'bpm', '124.0', 'rekordbox', ?, NULL, NULL)
            """,
            (_T0,),
        )
    finally:
        conn_b.close()

    conn_a = _open(spoke_a)
    try:
        stamped = _log(conn_a, "track_fields", ("trk-1", "bpm"), _DEV_A, _T1)
        conn_a.execute(
            """
            INSERT INTO track_fields(
                stable_id, field_name, value_json, source, modified_at,
                updated_at, origin_device_id
            )
            VALUES ('trk-1', 'bpm', '128.0', 'webui', ?, ?, ?)
            """,
            (_T1, stamped, _DEV_A),
        )
    finally:
        conn_a.close()

    # B goes first so the legacy row is what the hub sees initially; A's real
    # edit must still overwrite it, and B must then take A's value back.
    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    for data_dir in (spoke_a, spoke_b):
        conn = _open(data_dir)
        try:
            row = conn.execute(
                "SELECT value_json, updated_at FROM track_fields "
                "WHERE stable_id = 'trk-1' AND field_name = 'bpm'"
            ).fetchone()
            assert row is not None, data_dir.name
            assert row[0] == "128.0", data_dir.name
            assert row[1] == _T1, data_dir.name
        finally:
            conn.close()


def test_null_updated_at_sorts_as_epoch() -> None:
    """The unit rule under the legacy behaviour, stated once, directly."""
    legacy = protocol.lww_key({"updated_at": None, "origin_device_id": None})
    real = protocol.lww_key({"updated_at": _T0, "origin_device_id": _DEV_A})
    assert legacy == (protocol.EPOCH, protocol.NO_ORIGIN)
    assert legacy < real


#
# test_status_and_digest_endpoints_answer, test_every_lww_table_carries_the_
# sync_trio, test_push_from_an_unregistered_machine_is_refused,
# test_a_peer_on_another_schema_version_is_refused and
# test_webui_app_exposes_the_sync_router moved to
# tests/cloudsync/test_hub_sync_protocol.py (round 4 quality-gate file_size
# ratchet: this file crossed 600 lines). They import their fixtures and
# helpers back from this module.
