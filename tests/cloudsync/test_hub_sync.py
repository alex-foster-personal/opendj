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
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.sync_hub import client, engine, protocol, service
from apps.webui.server.app import create_app

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


class _TestClientTransport:
    """A :class:`apps.sync_hub.client.HubTransport` backed by ``TestClient``.

    Not a mock of the hub: it drives the real router through the real ASGI
    stack. It exists only because ``TestClient`` is not a URL.
    """

    def __init__(self, http: TestClient) -> None:
        self._http = http

    def _decoded(self, response: Any, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise client.SyncTransportError(
                f"{label} -> HTTP {response.status_code}: {response.text}"
            )
        return dict(response.json())

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._decoded(self._http.post(path, json=dict(payload)), f"POST {path}")

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        return self._decoded(
            self._http.get(path, params=dict(params)), f"GET {path}"
        )


# ----- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
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


def test_status_and_digest_endpoints_answer(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """Agent parity: the hub's read surface works without a spoke driving it."""
    _seed_common_track((spoke_a,), "trk-1")
    result = _sync(spoke_a, hub, "spoke-a")

    status = hub.get(
        f"{client.API_PREFIX}/status", {"machine_id": result.machine_id}
    )
    assert status["seq"] >= 1
    assert status["row_counts"]["tracks"] == 1
    assert {machine["name"] for machine in status["machines"]} >= {"hub", "spoke-a"}

    digest = protocol.SyncDigest.from_wire(
        hub.get(f"{client.API_PREFIX}/digest", {"machine_id": result.machine_id})
    )
    assert set(digest.tables) == set(protocol.DIGEST_TABLES)
    assert len(digest.overall) == 64


def test_every_lww_table_carries_the_sync_trio(spoke_a: Path) -> None:
    """Tripwire: a table joins the sync set only with the v6 sync columns.

    Without this, adding a table to ``SYNC_TABLES`` that lacks
    ``updated_at`` / ``origin_device_id`` / ``deleted_at`` would make every
    one of its rows look epoch-old and silently lose every conflict.
    """
    conn = _open(spoke_a)
    try:
        for spec in protocol.SYNC_TABLES:
            columns = set(protocol.table_columns(conn, spec.name))
            missing = [c for c in protocol.SYNC_COLUMNS if c not in columns]
            assert not missing, f"{spec.name} lacks {missing}"
            assert set(spec.pk) <= columns, spec.name
        membership = set(protocol.table_columns(conn, protocol.MEMBERSHIP_TABLE))
        assert set(protocol.SYNC_COLUMNS) <= membership
    finally:
        conn.close()


def test_push_from_an_unregistered_machine_is_refused(
    hub: _TestClientTransport,
) -> None:
    """A pusher that skipped ``hello`` gets a 409, not an anonymous write."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(
            f"{client.API_PREFIX}/push",
            {
                "machine_id": "deadbeef" * 4,
                "schema_version": state_schema.SCHEMA_VERSION,
                "rows": [],
            },
        )
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_a_peer_on_another_schema_version_is_refused(
    hub: _TestClientTransport,
) -> None:
    """Version skew fails at the handshake, not halfway through a row."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(
            f"{client.API_PREFIX}/hello",
            {
                "machine": {
                    "machine_id": "cafe" * 8,
                    "name": "future-spoke",
                    "platform": "linux",
                    "is_hub": False,
                    "data_root": "/tmp/future",
                    "first_seen": _T0,
                    "last_seen": _T0,
                },
                "schema_version": state_schema.SCHEMA_VERSION + 1,
            },
        )
    assert "SYNC_SCHEMA_VERSION" in str(excinfo.value)


def test_webui_app_exposes_the_sync_router() -> None:
    """The one-line wire-up in app.py actually mounts the endpoints."""
    app = create_app(mount_frontend=False, enable_cors=False)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert f"{client.API_PREFIX}/hello" in paths
    assert f"{client.API_PREFIX}/push" in paths
    assert f"{client.API_PREFIX}/pull" in paths
    assert f"{client.API_PREFIX}/status" in paths
    assert f"{client.API_PREFIX}/digest" in paths
