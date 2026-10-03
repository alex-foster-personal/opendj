"""PAIR-04: deleting an HTTP pairing keeps a graph edge it did not create.

[if] a graph edge predates an HTTP pairing on its endpoints [then] capture and delete leave it intact, [else stop].

Codex P2 (BLOCKING) on PR #4014 (thread r4171390101): capture coalesced onto an
existing ``pairings`` edge, for example one authored through the pairing CLI,
and the HTTP delete then removed that shared row unconditionally. Real migrated
state.db, real backend, no mocks.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.pairings.repo import PairingsRepo
from apps.shared.state import db as state_db
from apps.webui.server.backend import Pairing
from apps.webui.server.etag import compute_etag
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("PAIR-04")


def _route_pairing(pairing_id: str = "pair-1", notes: str | None = "from capture") -> Pairing:
    # Stamped the way routes/pairings.py stamps a create: server UTC, now.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return Pairing(
        pairing_id=pairing_id, from_stable_id="track-a", to_stable_id="track-b",
        direction="->", source="manual", notes=notes, snapshot=None,
        created_at=now, updated_at=now,
    )


@pytest.fixture
def backend_and_path(tmp_path: Path) -> tuple[SqliteBackend, Path]:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return SqliteBackend(path), path


def _graph(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT from_stable_id, to_stable_id, direction, source, notes FROM pairings"
        ).fetchall()
    finally:
        conn.close()


def _delete(backend: SqliteBackend, stored: Pairing) -> None:
    backend.delete_pairing(
        stored.pairing_id, expected_etag=compute_etag(stored.pairing_id, stored.updated_at)
    )


def test_delete_keeps_a_graph_edge_authored_before_the_http_pairing(backend_and_path) -> None:
    """[if] the CLI authored the edge first [then] capture and delete leave it as it was, [else stop]."""
    backend, path = backend_and_path
    conn = state_db.open_rw(path)
    try:
        PairingsRepo(conn).add("track-a", "track-b", direction="into", source="learned", notes="cli note")
        conn.commit()
    finally:
        conn.close()
    before = _graph(path)
    assert before == [("track-a", "track-b", "into", "learned", "cli note")]

    stored = backend.create_pairing(_route_pairing())
    assert _graph(path) == before, "capture must not rewrite an edge it does not own"
    _delete(backend, stored)

    assert _graph(path) == before, "delete must not remove an edge it does not own"


def test_delete_removes_the_graph_edge_the_http_pairing_created(backend_and_path) -> None:
    """[if] the HTTP pairing created the edge [then] deleting it removes the edge too, [else stop]."""
    backend, path = backend_and_path
    stored = backend.create_pairing(_route_pairing())
    assert _graph(path) == [("track-a", "track-b", "into", "manual", "from capture")]

    _delete(backend, stored)

    assert _graph(path) == []


def test_a_merged_capture_still_mirrors_onto_the_edge_it_created(backend_and_path) -> None:
    """[if] a second capture merges notes into an HTTP-created edge [then] the graph follows it, [else stop]."""
    backend, path = backend_and_path
    backend.create_pairing(_route_pairing(notes="first"))
    merged = backend.create_pairing(_route_pairing(pairing_id="pair-2", notes="second"))

    assert merged.pairing_id == "pair-1"
    assert _graph(path) == [("track-a", "track-b", "into", "manual", "first\nsecond")]
