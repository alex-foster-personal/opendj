"""Library organization foundation (PRD R-UNDO-1/2, R-API-2, s6 invariants)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import library_org as org
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

S = "session-a"


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[StateWriter]:
    conn = state_db.open_rw(tmp_path / "state.db")
    state_schema.apply_migrations(conn)
    (tmp_path / "machine-id").write_text("a" * 32)
    w = StateWriter(conn, FakeEventBus(), actor="test")
    w.insert_playlist(playlist_id="folder", name="Viva", vendor="local", vendor_pl_id="1")
    w.insert_playlist(playlist_id="list", name="Violins", vendor="local", vendor_pl_id="2")
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES ('t', 'inferred', 'T', '2026-10-30T00:00:00Z', '2026-10-30T00:00:00Z')"
    )
    yield w
    conn.close()


def _folder(w: StateWriter) -> None:
    org.organize_playlist(w, S, "folder", {"kind": "folder"})


def test_tag_types_are_seeded(writer: StateWriter) -> None:
    rows = {r["id"]: r for r in org.list_rows(writer._conn, "tag_type")}
    assert set(rows) == {"general", "instrument", "mood"}
    assert (rows["instrument"]["show_as_column"], rows["instrument"]["show_in_tags"]) == (1, 0)


def test_a_list_nests_only_in_a_folder_and_never_in_itself(writer: StateWriter) -> None:
    with pytest.raises(org.OrgError) as refused:
        org.organize_playlist(writer, S, "list", {"parent_id": "folder"})
    assert refused.value.code == "INVARIANT"
    _folder(writer)
    assert org.organize_playlist(writer, S, "list", {"parent_id": "folder"}).row["parent_id"] == "folder"
    org.organize_playlist(writer, S, "list", {"kind": "folder"})
    with pytest.raises(org.OrgError) as cycle:
        org.organize_playlist(writer, S, "folder", {"parent_id": "list"})
    assert cycle.value.code == "INVARIANT"


def test_a_set_needs_a_parent_and_owns_set_status(writer: StateWriter) -> None:
    with pytest.raises(org.OrgError):
        org.organize_playlist(writer, S, "list", {"kind": "set"})
    with pytest.raises(org.OrgError):
        org.organize_playlist(writer, S, "list", {"set_status": "draft"})
    row = org.organize_playlist(writer, S, "list", {"kind": "set", "parent_id": "folder", "set_status": "draft"}).row
    assert row["set_status"] == "draft"


def test_a_bad_stage_is_validation_not_a_500(writer: StateWriter) -> None:
    with pytest.raises(org.OrgError) as refused:
        org.organize_playlist(writer, S, "list", {"stage": "vetted"})
    assert refused.value.code == "VALIDATION"


def test_blank_section_title_stays_new_section(writer: StateWriter) -> None:
    row = org.add_section(writer, S, owner_kind="playlist", owner_id="list", before_position=3, title="  ").row
    assert row["title"] == "New section"


def test_identical_write_is_a_no_op_with_no_undo(writer: StateWriter) -> None:
    assert org.put_tag_meta(writer, S, " Piano ").undo_id is not None
    again = org.put_tag_meta(writer, S, "piano ")
    assert again.row["name_norm"] == "piano"
    assert len(org.list_rows(writer._conn, "tag_meta")) == 1


def test_undo_reverts_an_insert_and_an_update(writer: StateWriter) -> None:
    section = org.add_section(writer, S, owner_kind="playlist", owner_id="list", before_position=0, title="Peak").row
    org.update_section(writer, S, section["id"], {"title": "Closers"})
    assert org.undo_last(writer, S).op == "section.update"
    rows = org.list_rows(writer._conn, "playlist_section")
    assert [r["title"] for r in rows] == ["Peak"]
    assert org.undo_last(writer, S).op == "section.add"
    assert org.list_rows(writer._conn, "playlist_section") == []


def test_undo_restores_a_delete(writer: StateWriter) -> None:
    work = org.create_work(writer, S, {"kind": "song", "title": "Crazy in Love"}).row
    org.link_member(writer, S, work["id"], member_kind="track", member_id="t", role="original")
    org.delete_work(writer, S, work["id"])
    assert org.list_rows(writer._conn, "work") == []
    org.undo_last(writer, S)
    assert len(org.list_rows(writer._conn, "work_member")) == 1
    assert len(org.list_rows(writer._conn, "work")) == 1


def test_fifty_operations_all_undo(writer: StateWriter) -> None:
    for index in range(50):
        org.put_tag_meta(writer, S, f"tag{index}")
    for _ in range(50):
        org.undo_last(writer, S)
    assert org.list_rows(writer._conn, "tag_meta") == []


def test_undo_refuses_a_row_changed_since_and_keeps_the_entry(writer: StateWriter) -> None:
    section = org.add_section(writer, S, owner_kind="playlist", owner_id="list", before_position=0).row
    org.update_section(writer, "other-session", section["id"], {"title": "Theirs"})
    with pytest.raises(org.OrgError) as refused:
        org.undo_last(writer, S)
    assert refused.value.code == "CONFLICT"
    org.undo_last(writer, "other-session")
    assert org.undo_last(writer, S).op == "section.add"


def test_undo_is_per_session(writer: StateWriter) -> None:
    org.put_tag_meta(writer, S, "piano")
    with pytest.raises(org.OrgError) as refused:
        org.undo_last(writer, "other-session")
    assert refused.value.code == "NOT_FOUND"


def test_a_new_primary_demotes_the_old_one(writer: StateWriter) -> None:
    first = org.create_work(writer, S, {"kind": "song", "title": "A"}).row["id"]
    second = org.create_work(writer, S, {"kind": "song", "title": "B"}).row["id"]
    for work in (first, second):
        org.link_member(writer, S, work, member_kind="track", member_id="t", role="remix", is_primary=True)
    primaries = org.list_rows(writer._conn, "work_member", {"is_primary": 1})
    assert [r["work_id"] for r in primaries] == [second]


def test_a_dismissed_pair_is_unordered(writer: StateWriter) -> None:
    org.dismiss_pair(writer, S, "b", "a")
    org.dismiss_pair(writer, S, "a", "b")
    assert len(org.list_rows(writer._conn, "work_dismissal")) == 1


def test_every_write_reaches_the_sync_changelog(writer: StateWriter) -> None:
    org.put_tag_meta(writer, S, "piano")
    logged = writer._conn.execute("SELECT COUNT(*) FROM local_changelog WHERE table_name = 'tag_meta'").fetchone()[0]
    assert logged == 1
