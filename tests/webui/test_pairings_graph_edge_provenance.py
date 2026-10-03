"""PAIR-04: deleting an HTTP pairing keeps a graph edge it did not create.

[if] a graph edge predates an HTTP pairing on its endpoints [then] capture and delete leave it intact, [else stop].

Codex P2 (BLOCKING) on PR #4014 (thread r4171390101): capture coalesced onto an
existing ``pairings`` edge, for example one authored through the pairing CLI,
and the HTTP delete then removed that shared row unconditionally. Silver P0 on
the first fix: ownership by "the edge is newer" also claimed an edge that graph
tooling recreated after the capture. Ownership is now an explicit marker,
``http_pairings.graph_owner_stamp``. Real migrated state.db, real backend, no mocks.
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


def _cli_rewrite(path: Path, *, recreate: bool) -> None:
    conn = state_db.open_rw(path)
    try:
        repo = PairingsRepo(conn)
        if recreate:
            repo.remove("track-a", "track-b", "into")
        repo.add("track-a", "track-b", direction="into", source="learned", notes="tool note")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("recreate", [True, False], ids=["recreated", "rewritten"])
def test_delete_keeps_an_edge_graph_tooling_wrote_after_the_capture(
    backend_and_path, recreate: bool
) -> None:
    """[if] graph tooling recreates or rewrites the edge after capture [then] delete leaves its version, [else stop]."""
    backend, path = backend_and_path
    stored = backend.create_pairing(_route_pairing())
    _cli_rewrite(path, recreate=recreate)
    before = _graph(path)
    assert before == [("track-a", "track-b", "into", "learned", "tool note")]

    _delete(backend, stored)

    assert _graph(path) == before


def test_a_capture_after_tooling_rewrote_the_edge_does_not_overwrite_it(backend_and_path) -> None:
    """[if] tooling rewrote an HTTP-created edge [then] a later merged capture leaves the edge alone, [else stop]."""
    backend, path = backend_and_path
    backend.create_pairing(_route_pairing(notes="first"))
    _cli_rewrite(path, recreate=False)
    backend.create_pairing(_route_pairing(pairing_id="pair-2", notes="second"))

    assert _graph(path) == [("track-a", "track-b", "into", "learned", "tool note")]


def test_rows_from_before_the_marker_own_no_edge(tmp_path: Path) -> None:
    """[if] an http_pairings table predates graph_owner_stamp [then] it gains the column and its rows own no edge, [else stop]."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    try:
        conn.execute(
            "CREATE TABLE http_pairings (pairing_id TEXT PRIMARY KEY, from_stable_id TEXT NOT NULL, "
            "to_stable_id TEXT NOT NULL, direction TEXT NOT NULL, source TEXT NOT NULL, notes TEXT, "
            "snapshot_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        stamp = "2026-09-26T00:00:00.000000Z"
        conn.execute(
            "INSERT INTO http_pairings VALUES ('pair-1', 'track-a', 'track-b', '->', 'manual', "
            "'old', NULL, ?, ?)",
            (stamp, stamp),
        )
        PairingsRepo(conn).add("track-a", "track-b", direction="into", source="manual", notes="old")
        conn.commit()
    finally:
        conn.close()
    backend = SqliteBackend(path)
    (stored,) = backend.list_pairings()
    before = _graph(path)

    _delete(backend, stored)

    assert _graph(path) == before
