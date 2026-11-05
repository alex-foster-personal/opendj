"""Seeded rows stay off the wire until someone edits them (migrations_v26)."""

from __future__ import annotations

from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.library_org import update_tag_type
from apps.shared.state.writer import StateWriter
from apps.sync_hub.engine_changes import _rows_for_table
from apps.sync_hub.protocol import SYNC_TABLES
from apps.sync_hub.sync_set import HeldKeys


def _offered_tag_types(path: Path) -> list[object]:
    conn = state_db.open_rw(path)
    try:
        spec = next(spec for spec in SYNC_TABLES if spec.name == "tag_type")
        return [change.pk for change in _rows_for_table(conn, spec, HeldKeys(conn)).rows]
    finally:
        conn.close()


def test_a_fresh_library_offers_no_seeded_tag_type(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    conn.close()
    assert _offered_tag_types(path) == []


def test_an_edited_seed_is_offered(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    (tmp_path / "machine-id").write_text("a" * 32)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    update_tag_type(writer, "s", "mood", {"show_as_column": 1})
    writer.close()
    conn.close()
    assert len(_offered_tag_types(path)) == 1
