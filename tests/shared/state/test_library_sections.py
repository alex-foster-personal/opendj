"""Sections among tracks (R-SEC-1, R-SEC-3, R-DND-1, E18, E40).

[if] a move or a membership write separates a divider from its tracks [then] the build is broken, [else stop].
[if] a section move does not undo as one block [then] the build is broken, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import library_entries as entries
from apps.shared.state import library_org as org
from apps.shared.state import library_sections as layout
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.library_sections import Member, Token
from apps.shared.state.writer import StateWriter

pytestmark = [
    pytest.mark.requirement("LIBSEC-7909"),
    pytest.mark.requirement("LIBSEC-7909b"),
]


S = "session-a"
T: Literal["track"] = "track"
H: Literal["section"] = "section"


def _section(section_id: str, position: int, created: str = "1") -> dict[str, object]:
    return {"id": section_id, "before_position": position, "created_at": created}


def test_a_section_renders_above_its_track_and_past_the_end_at_the_bottom() -> None:
    tokens = layout.display_tokens(["a", "b"], [_section("x", 1), _section("y", 9)])
    assert tokens == [Token(T, "a"), Token(H, "x"), Token(T, "b"), Token(H, "y")]


def test_two_dividers_on_one_index_keep_creation_order() -> None:
    tokens = layout.display_tokens(["a"], [_section("b", 0, "2"), _section("a", 0, "1")])
    assert [t.id for t in tokens] == ["a", "b", "a"]


def test_a_mixed_selection_moves_as_a_block_in_display_order() -> None:
    tokens = layout.display_tokens(["a", "b", "c", "d"], [])
    moved = layout.move_tokens(tokens, {Token(T, "d"), Token(T, "a")}, Token(T, "c"))
    assert layout.track_order(moved) == ["b", "a", "d", "c"]


def test_a_drop_on_the_dragged_rows_leaves_everything_in_place() -> None:
    tokens = layout.display_tokens(["a", "b", "c"], [])
    assert layout.move_tokens(tokens, {Token(T, "b")}, Token(T, "b")) == tokens


def test_a_track_can_land_either_side_of_a_divider() -> None:
    tokens = layout.display_tokens(["a", "b", "c"], [_section("x", 2)])
    above = layout.move_tokens(tokens, {Token(T, "a")}, Token(H, "x"))
    below = layout.move_tokens(tokens, {Token(T, "a")}, Token(T, "c"))
    assert (layout.track_order(above), layout.section_positions(above)) == (["b", "a", "c"], {"x": 2})
    assert (layout.track_order(below), layout.section_positions(below)) == (["b", "a", "c"], {"x": 1})


def test_an_unknown_line_is_refused() -> None:
    with pytest.raises(KeyError):
        layout.move_tokens(layout.display_tokens(["a"], []), {Token(T, "nope")}, None)


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (["a", "c"], 1),  # the anchor went: the next survivor holds it
        (["c", "b", "a"], 1),  # the anchor moved: the divider follows it
        (["a", "n", "b", "c"], 2),  # a track added above stays above
        ([], 0),  # nothing left: the end of an empty list
    ],
)
def test_a_divider_stays_on_its_track_across_other_writes(after: list[str], expected: int) -> None:
    before = [Member(i, i) for i in "abc"]
    now = [Member(i, i) for i in after]
    assert layout.follow_members(before, now, [_section("x", 1)]) == {"x": expected}


def test_a_second_copy_of_a_track_does_not_capture_a_divider() -> None:
    before = [Member("1", "a"), Member("2", "a"), Member("3", "b")]
    after = [Member("1", "a"), Member("3", "b")]
    assert layout.follow_members(before, after, [_section("x", 1)]) == {"x": 1}


def test_a_full_replace_is_matched_by_stable_id() -> None:
    placed = layout.follow_members(
        [Member("1", "a"), Member("2", "b")],
        [Member("8", "b"), Member("9", "a")],
        [_section("x", 1)],
    )
    assert placed == {"x": 0}


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[StateWriter]:
    conn = state_db.open_rw(tmp_path / "state.db")
    state_schema.apply_migrations(conn)
    (tmp_path / "machine-id").write_text("a" * 32)
    w = StateWriter(conn, FakeEventBus(), actor="test")
    w.insert_playlist(playlist_id="p", name="Viva", vendor="local", vendor_pl_id="1")
    for stable_id in "abcd":
        w.upsert_track(
            stable_id=stable_id,
            stable_id_tier="isrc",
            title=stable_id,
            artists=["x"],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=None,
        )
    w.set_playlist_memberships("p", list("abcd"))
    yield w
    conn.close()


def _items(w: StateWriter) -> dict[str, str]:
    return {m["stable_id"]: m["item_id"] for m in entries._live_members(w._conn, "p")}


def _order(w: StateWriter) -> str:
    return "".join(m["stable_id"] for m in entries._live_members(w._conn, "p"))


def _add_section(w: StateWriter, position: int) -> str:
    return org.add_section(w, S, owner_kind="playlist", owner_id="p", before_position=position).row["id"]


def _row(w: StateWriter, table: str, key: dict[str, Any]) -> dict[str, Any]:
    row = org._read(w._conn, table, key)
    assert row is not None
    return row


def _position(w: StateWriter, section_id: str) -> int:
    return _row(w, "playlist_section", {"id": section_id})["before_position"]


def _revision(w: StateWriter) -> str:
    return _row(w, "playlists", {"playlist_id": "p"})["updated_at"]


def test_moving_a_section_moves_its_tracks_and_undo_restores_both(writer: StateWriter) -> None:
    items = _items(writer)
    section = _add_section(writer, 2)
    revision = _revision(writer)
    result = entries.move_entries(
        writer,
        S,
        "p",
        item_ids=[items["c"], items["d"]],
        section_ids=[section],
        before_item_id=items["a"],
    )
    assert _order(writer) == "cdab"
    assert _position(writer, section) == 0
    assert _revision(writer) != revision
    assert result.row["sections"][0]["id"] == section
    undone = org.undo_last(writer, S)
    assert undone.op == "entries.move"
    assert _order(writer) == "abcd"
    assert _position(writer, section) == 2


def test_a_move_that_changes_nothing_has_no_undo(writer: StateWriter) -> None:
    items = _items(writer)
    result = entries.move_entries(writer, S, "p", item_ids=[items["b"]], section_ids=[], before_item_id=items["c"])
    assert result.undo_id is None
    assert _order(writer) == "abcd"


def test_moving_only_a_divider_leaves_every_track_alone(writer: StateWriter) -> None:
    items = _items(writer)
    section = _add_section(writer, 1)
    entries.move_entries(writer, S, "p", item_ids=[], section_ids=[section], before_item_id=items["d"])
    assert (_order(writer), _position(writer, section)) == ("abcd", 3)


def test_a_divider_dropped_above_a_tied_divider_reads_above_it(writer: StateWriter) -> None:
    first = _add_section(writer, 1)
    second = _add_section(writer, 1)
    entries.move_entries(writer, S, "p", item_ids=[], section_ids=[second], before_section_id=first)
    rows = entries.list_sections(writer._conn, "p")
    assert [r["id"] for r in layout.ordered_sections(rows)] == [second, first]
    assert org.undo_last(writer, S).op == "entries.move"
    rows = entries.list_sections(writer._conn, "p")
    assert [r["id"] for r in layout.ordered_sections(rows)] == [first, second]


def test_a_move_past_the_end_appends_and_a_foreign_item_is_refused(writer: StateWriter) -> None:
    items = _items(writer)
    entries.move_entries(writer, S, "p", item_ids=[items["a"]], section_ids=[])
    assert _order(writer) == "bcda"
    with pytest.raises(org.OrgError) as refused:
        entries.move_entries(writer, S, "p", item_ids=["nope"], section_ids=[])
    assert refused.value.code == "NOT_FOUND"


def test_a_narrow_gap_renumbers_and_keeps_the_order(writer: StateWriter) -> None:
    items = _items(writer)
    for stable_id, key in (("a", "1"), ("b", "1" + "0" * 63), ("c", "2"), ("d", "3")):
        writer._conn.execute("UPDATE playlist_memberships SET order_key = ? WHERE stable_id = ?", (key, stable_id))
    entries.move_entries(writer, S, "p", item_ids=[items["d"]], section_ids=[], before_item_id=items["b"])
    assert _order(writer) == "adbc"


def test_a_divider_follows_when_a_track_above_it_is_removed(writer: StateWriter) -> None:
    section = _add_section(writer, 2)
    before = entries.live_member_order(writer._conn, "p")
    writer.set_playlist_memberships("p", list("bcd"))
    entries.follow_membership_change(writer, "p", before)
    assert _position(writer, section) == 1


def test_a_list_with_no_sections_reads_no_members(writer: StateWriter) -> None:
    assert entries.live_member_order(writer._conn, "p") == []


def test_a_set_owns_its_sections_as_a_set(writer: StateWriter) -> None:
    """[if] a kind=set list keeps sections as owner_kind=set [then] moves find them, [else stop]."""
    writer.insert_playlist(playlist_id="f", name="Viva", vendor="local", vendor_pl_id="2")
    org.organize_playlist(writer, S, "f", {"kind": "folder"})
    org.organize_playlist(writer, S, "p", {"kind": "set", "parent_id": "f"})
    items = _items(writer)
    section = org.add_section(writer, S, owner_kind="set", owner_id="p", before_position=1).row["id"]
    assert entries.section_owner_kind(writer._conn, "p") == "set"
    assert [r["id"] for r in entries.list_sections(writer._conn, "p")] == [section]
    entries.move_entries(writer, S, "p", item_ids=[items["a"]], section_ids=[], before_item_id=items["c"])
    assert (_order(writer), _position(writer, section)) == ("bacd", 0)
