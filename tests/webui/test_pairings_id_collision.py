"""PAIR-04: a create that reuses a pairing_id for another edge is refused (409).

Sol P1 on PR #4014 (thread r4166140716): a create whose pairing_id already
names a pairing with different endpoints or direction missed ``_find_existing``
and reached the ``ON CONFLICT(pairing_id) DO UPDATE`` upsert, overwriting the
original pairing and its snapshot while the CAT-03 graph mirror kept the old
edge beside the new one. Real migrated state.db, real backends, no mocks.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server.backend import InMemoryBackend, Pairing
from apps.webui.server.pairings_sqlite import PairingIdConflictError
from apps.webui.server.playlist_add import AlreadyExistsError
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("PAIR-04")

STAMP = "2026-10-02T00:00:00.000000Z"
SNAPSHOT = {"captured": "original"}


def _pairing(from_id: str, to_id: str, direction: str = "->", **kw: object) -> Pairing:
    return Pairing(
        pairing_id="pair-1", from_stable_id=from_id, to_stable_id=to_id,
        direction=direction, source="manual", notes=kw.get("notes"),  # type: ignore[arg-type]
        snapshot=kw.get("snapshot"),  # type: ignore[arg-type]
        created_at=STAMP, updated_at=STAMP,
    )


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> tuple[SqliteBackend, Path]:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return SqliteBackend(path), path


def _rows(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    conn = sqlite3.connect(path)
    try:
        http = conn.execute(
            "SELECT pairing_id, from_stable_id, to_stable_id, direction, snapshot_json "
            "FROM http_pairings ORDER BY pairing_id"
        ).fetchall()
        graph = conn.execute(
            "SELECT from_stable_id, to_stable_id FROM pairings ORDER BY 1, 2"
        ).fetchall()
    finally:
        conn.close()
    return http, graph


@pytest.mark.parametrize(
    "collision",
    [("track-c", "track-d", "->"), ("track-a", "track-b", "<->"), ("track-b", "track-a", "->")],
)
def test_sqlite_create_rejects_a_reused_pairing_id(sqlite_backend, collision) -> None:
    """[if] a create reuses a stored pairing_id for another edge [then] it is refused and both tables are unchanged [else stop]."""
    backend, path = sqlite_backend
    backend.create_pairing(_pairing("track-a", "track-b", snapshot=SNAPSHOT))
    before = _rows(path)

    with pytest.raises(PairingIdConflictError) as exc:
        backend.create_pairing(_pairing(*collision))

    assert isinstance(exc.value, AlreadyExistsError)  # served as 409 already_exists
    assert _rows(path) == before
    assert before[0][0][1:4] == ("track-a", "track-b", "->")


def test_sqlite_same_edge_create_still_merges(sqlite_backend) -> None:
    """Control: the same id on the same edge is the idempotent merge, not a conflict."""
    backend, path = sqlite_backend
    backend.create_pairing(_pairing("track-a", "track-b"))
    merged = backend.create_pairing(_pairing("track-a", "track-b", notes="again"))
    assert merged.notes == "again"
    assert len(_rows(path)[0]) == 1


def test_in_memory_backend_matches(tmp_path: Path) -> None:
    """The in-memory backend refuses the same collision, and still accepts a new id."""
    backend = InMemoryBackend()
    backend.create_pairing(_pairing("track-a", "track-b"))
    with pytest.raises(PairingIdConflictError):
        backend.create_pairing(_pairing("track-c", "track-d"))
    fresh = Pairing(
        pairing_id="pair-2", from_stable_id="track-c", to_stable_id="track-d",
        direction="->", source="manual", notes=None,
    )
    assert backend.create_pairing(fresh).pairing_id == "pair-2"
