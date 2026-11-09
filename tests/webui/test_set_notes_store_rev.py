"""Store-layer set-notes revision return (issue #9299).

[if] a peer writes during save_set_note [then] the caller keeps its body and rev, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.set_notes.store import Geometry, StaleRevision, load_set_note, open_set_notes, save_set_note
from apps.shared.state import db as state_db
from tests.webui.set_notes_test_db import init_empty_state_db

pytestmark = pytest.mark.requirement("SETNOTE-6340c")


def test_save_set_note_returns_this_write_when_peer_updates_before_readback(tmp_path: Path) -> None:
    """[if] a second writer lands after UPDATE [then] the return is this write, next save is stale, [else stop]."""
    db = tmp_path / "state.db"
    init_empty_state_db(db)

    seed = open_set_notes(db)
    try:
        save_set_note(
            seed,
            "scratch",
            "seed",
            Geometry(x=0, y=0, w=100, h=100),
            True,
            base_rev=0,
        )
    finally:
        seed.close()

    geom_a = Geometry(x=10, y=20, w=300, h=200)
    geom_b = Geometry(x=11, y=21, w=301, h=201)

    def _conn1_class(geom_peer: Geometry, db_path: Path) -> type[sqlite3.Connection]:
        class _Conn1(sqlite3.Connection):
            """Runs a second writer after conn1's set_notes UPDATE has committed."""

            _inject_once = True

            def execute(self, sql: str, parameters=()) -> sqlite3.Cursor:  # type: ignore[override]
                cur = sqlite3.Connection.execute(self, sql, parameters)
                if (
                    _Conn1._inject_once
                    and "UPDATE set_notes SET body_md = ?" in sql
                    and "WHERE scope = ? AND rev = ?" in sql
                ):
                    _Conn1._inject_once = False
                    conn2 = open_set_notes(db_path)
                    try:
                        save_set_note(conn2, "scratch", "B", geom_peer, False, base_rev=2)
                    finally:
                        conn2.close()
                return cur

        return _Conn1

    _orig_connect = state_db.sqlite3.connect

    def _connect_conn1_factory(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs = dict(kwargs)
        kwargs["factory"] = _conn1_class(geom_b, db)
        return _orig_connect(*args, **kwargs)

    state_db.sqlite3.connect = _connect_conn1_factory
    try:
        conn1 = open_set_notes(db)
    finally:
        state_db.sqlite3.connect = _orig_connect

    returned = save_set_note(conn1, "scratch", "A", geom_a, True, base_rev=1)

    assert returned.body_md == "A"
    assert returned.rev == 2
    assert returned.visible is True
    assert returned.geometry.x == geom_a.x
    assert returned.geometry.y == geom_a.y
    assert returned.geometry.w == geom_a.w
    assert returned.geometry.h == geom_a.h

    with pytest.raises(StaleRevision) as excinfo:
        save_set_note(conn1, "scratch", "A-again", geom_a, True, base_rev=2)
    assert excinfo.value.current_rev == 3

    on_disk = load_set_note(conn1, "scratch")
    assert on_disk.body_md == "B"
    assert on_disk.rev == 3
    assert on_disk.visible is False

    conn1.close()


def test_save_set_note_return_geometry_does_not_alias_input(tmp_path: Path) -> None:
    """[if] save_set_note succeeds [then] mutating input geometry leaves return unchanged, [else stop]."""
    db = tmp_path / "state.db"
    init_empty_state_db(db)
    conn = open_set_notes(db)
    try:
        geom = Geometry(x=1, y=2, w=3, h=4)
        returned = save_set_note(conn, "scratch", "body", geom, True, base_rev=0)
        geom.x = 99
        assert returned.geometry.x == 1
    finally:
        conn.close()
