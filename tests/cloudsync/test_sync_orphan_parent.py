"""A child row whose parent this machine no longer stores is held, not offered.

Found live on silver (Thu 5 Nov 2026): 209 ``track_vendor_ids`` rows named
``spotify-pending:*`` tracks that the same ``state.db`` no longer stores. The
offer treated a missing parent as "free to go", the hub answered the whole
push chunk with ``FOREIGN KEY constraint failed`` (HTTP 409 SYNC_APPLY), and
every retry re-fired it, so the spoke could neither push nor pull::

    POST /api/v1/sync/push -> HTTP 409: track_vendor_ids: applying row
    ['spotify-pending:01335b36...', 'spotify'] violates a constraint
    (FOREIGN KEY constraint failed)

SQLite only enforces a foreign key on a connection that turned it on, so a
writer that deleted a parent with ``foreign_keys=OFF`` leaves the child behind.

Acceptance criteria, one test each:
- if a full offer (first sync) sends an orphan child, the push is a 409 that no
  retry gets past -- broken.
- if a fenced offer (every later sync) sends an orphan child, same -- broken.
- if the orphan is held but reported as a held PARENT rather than a missing
  one, the operator is pointed at a root hold that does not exist -- broken.

[if] an orphan child reaches the wire, or is reported under the wrong kind [then] fail, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client, service, sync_set

from .test_hub_sync import (
    _DEV_A,
    _T0,
    _insert_track,
    _log,
    _open,
    _sync,
    _TestClientTransport,
)

pytestmark = pytest.mark.requirement("CAT-04")

_ORPHAN = "spotify-pending:01335b3613b4d97dda5deeb29ceb02cff92201c5"


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[_TestClientTransport]:
    """The real sync router over the real ASGI stack, on an EMPTY hub DB."""
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


def _insert_vendor_id(conn: sqlite3.Connection, stable_id: str, vendor_id: str) -> None:
    stamped = _log(conn, "track_vendor_ids", (stable_id, "spotify"), _DEV_A, _T0)
    conn.execute(
        "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id, "
        "updated_at, origin_device_id) VALUES (?, 'spotify', ?, ?, ?)",
        (stable_id, vendor_id, stamped, _DEV_A),
    )
    conn.commit()


def _delete_parent_without_fk(conn: sqlite3.Connection, stable_id: str) -> None:
    """What the silver writer did: drop the track with FK enforcement off."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM tracks WHERE stable_id = ?", (stable_id,))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def _seed_with_orphan(conn: sqlite3.Connection) -> None:
    _insert_track(conn, _ORPHAN, title="placeholder", updated_at=_T0, origin=_DEV_A)
    _insert_track(conn, "trk-control", title="control", updated_at=_T0, origin=_DEV_A)
    _insert_vendor_id(conn, _ORPHAN, "7A0ebSM5azZMIOskqpPKQf")
    _insert_vendor_id(conn, "trk-control", "133zydEnKTb4XGrIm2pdBf")
    _delete_parent_without_fk(conn, _ORPHAN)


def _hub_vendor_keys(tmp_path: Path) -> set[str]:
    conn = _open(tmp_path / "hub")
    try:
        return {str(row[0]) for row in conn.execute("SELECT stable_id FROM track_vendor_ids")}
    finally:
        conn.close()


def test_a_full_offer_holds_an_orphan_child_and_the_push_lands(tmp_path: Path, hub: _TestClientTransport) -> None:
    spoke = tmp_path / "spoke"
    conn = _open(spoke)
    try:
        _seed_with_orphan(conn)
    finally:
        conn.close()

    result = _sync(spoke, hub, "spoke")

    assert result.quarantined == 1, "exactly the orphan child is held back"
    hub_keys = _hub_vendor_keys(tmp_path)
    assert "trk-control" in hub_keys, "control: the sibling with a stored parent travels"
    assert _ORPHAN not in hub_keys


def test_a_fenced_offer_holds_an_orphan_child_and_the_push_lands(tmp_path: Path, hub: _TestClientTransport) -> None:
    spoke = tmp_path / "spoke"
    conn = _open(spoke)
    try:
        _insert_track(conn, "trk-control", title="control", updated_at=_T0, origin=_DEV_A)
        conn.commit()
    finally:
        conn.close()
    first = _sync(spoke, hub, "spoke")
    assert first.quarantined == 0

    conn = _open(spoke)
    try:
        _insert_track(conn, _ORPHAN, title="placeholder", updated_at=_T0, origin=_DEV_A)
        _insert_vendor_id(conn, _ORPHAN, "7A0ebSM5azZMIOskqpPKQf")
        _insert_vendor_id(conn, "trk-control", "133zydEnKTb4XGrIm2pdBf")
        _delete_parent_without_fk(conn, _ORPHAN)
    finally:
        conn.close()

    second = _sync(spoke, hub, "spoke")

    assert second.quarantined == 1
    hub_keys = _hub_vendor_keys(tmp_path)
    assert "trk-control" in hub_keys
    assert _ORPHAN not in hub_keys


def test_an_orphan_child_is_reported_as_a_missing_parent(tmp_path: Path) -> None:
    conn = _open(tmp_path / "spoke")
    try:
        _seed_with_orphan(conn)
        by_kind = sync_set.exclusion_counts_by_kind(conn)
    finally:
        conn.close()

    assert by_kind.get("parent_missing") == 1
    assert not by_kind.get("parent_held")
    reason = f"its tracks parent {_ORPHAN} {sync_set.PARENT_MISSING_SUFFIX}"
    assert sync_set.exclusion_kind(reason) == "parent_missing"
    assert "normalize_stamps" not in sync_set.remedy_for(reason)
    assert sync_set.hold_group(reason) == f"its tracks parent {sync_set.PARENT_MISSING_SUFFIX}"
