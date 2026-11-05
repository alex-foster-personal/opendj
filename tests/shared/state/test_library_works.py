"""Works, stacks, suggestions and classical metadata (PRD R-VER, R-WORK, R-CLS, W1-W7).

[if] a suggestion links tracks by itself or ignores a dismissal [then] the build is broken, [else stop].
[if] a work rename, merge or stack order loses a member [then] the build is broken, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import library_org as org
from apps.shared.state import library_org_works as org_works
from apps.shared.state import library_works as works
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = [
    pytest.mark.requirement("LIBWORK-7901"),
    pytest.mark.requirement("LIBWORK-7901b"),
    pytest.mark.requirement("LIBWORK-7901c"),
]

S = "session-a"
T0 = "2026-10-30T00:00:00Z"


def _track(w: StateWriter, stable_id: str, title: str, artist: str = "X") -> None:
    w._conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, created_at, updated_at)"
        " VALUES (?, 'inferred', ?, ?, ?, ?)",
        (stable_id, title, json.dumps([artist]), T0, T0),
    )


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[StateWriter]:
    conn = state_db.open_rw(tmp_path / "state.db")
    state_schema.apply_migrations(conn)
    (tmp_path / "machine-id").write_text("a" * 32)
    w = StateWriter(conn, FakeEventBus(), actor="test")
    _track(w, "lac-a", "Lacrimosa (Leblanc Remix)", "Leblanc")
    _track(w, "lac-b", "Lacrimosa - ASCO Edit", "ASCO")
    _track(w, "tear", "Tearful Eyes", "Vik")
    _track(w, "ave-s", "Ave Maria", "Hendricks")
    _track(w, "ave-g", "Ave Maria", "Callas")
    conn.commit()
    yield w
    conn.close()


def _work(w: StateWriter, title: str = "Lacrimosa", composer: str | None = "Mozart") -> str:
    fields = {"kind": "classical", "title": title, "composer": composer}
    return org.create_work(w, S, fields).row["id"]


def _members(w: StateWriter, work_id: str) -> list[str]:
    return [m["member_id"] for m in works.stack(w._conn, _any_member(w, work_id))["members"]]


def _any_member(w: StateWriter, work_id: str) -> str:
    return org.list_rows(w._conn, "work_member", {"work_id": work_id})[0]["member_id"]


# ----- R-WORK-1, E16, E17: stacking ---------------------------------------------


def test_linking_a_selection_creates_one_work_and_touches_no_track(writer: StateWriter) -> None:
    before = writer._conn.execute("SELECT title, artists_json FROM tracks ORDER BY 1").fetchall()
    result = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa", role="remix")
    assert sorted(_members(writer, result.row["id"])) == ["lac-a", "lac-b"]
    after = writer._conn.execute("SELECT title, artists_json FROM tracks ORDER BY 1").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after], "W1: titles never change"


def test_linking_extends_the_existing_work_rather_than_making_a_second(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    assert org_works.link_tracks(writer, S, ["lac-a", "tear"]).row["id"] == work_id
    assert len(org.list_rows(writer._conn, "work")) == 1


def test_stacking_tracks_already_together_is_a_no_op(writer: StateWriter) -> None:
    org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa")
    assert org_works.link_tracks(writer, S, ["lac-b", "lac-a"]).undo_id is None


def test_two_different_works_are_never_resolved_silently(writer: StateWriter) -> None:
    a = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="One").row["id"]
    b = org_works.link_tracks(writer, S, ["ave-s", "ave-g"], title="Two").row["id"]
    with pytest.raises(org.OrgError) as refused:
        org_works.link_tracks(writer, S, ["lac-a", "ave-s"])
    assert refused.value.code == "CONFLICT"
    assert set(refused.value.details["work_ids"]) == {a, b}


def test_a_track_never_holds_two_primary_works(writer: StateWriter) -> None:
    first, second = _work(writer, "One"), _work(writer, "Two")
    for work_id in (first, second):
        org.link_member(writer, S, work_id, member_kind="track", member_id="tear", role="remix", is_primary=True)
    primaries = [
        m["work_id"] for m in org.list_rows(writer._conn, "work_member", {"member_id": "tear", "is_primary": 1})
    ]
    assert primaries == [second]


def test_a_mashup_keeps_secondary_works(writer: StateWriter) -> None:
    first, second = _work(writer, "Lacrimosa"), _work(writer, "Dies irae")
    org.link_member(writer, S, first, member_kind="track", member_id="tear", role="mashup", is_primary=True)
    org.link_member(writer, S, second, member_kind="track", member_id="tear", role="mashup", is_primary=False)
    links = works.overlay(writer._conn)["members"]["tear"]
    assert {(l["work_id"], l["is_primary"]) for l in links} == {(first, True), (second, False)}


def test_linking_a_track_that_does_not_exist_is_not_found(writer: StateWriter) -> None:
    with pytest.raises(org.OrgError) as refused:
        org.link_member(writer, S, _work(writer), member_kind="track", member_id="ghost", role="remix")
    assert refused.value.code == "NOT_FOUND"


# ----- R-VER-3, R-VER-4, W2: the stack ------------------------------------------


def test_stack_lists_every_member_with_this_row_role_and_place(writer: StateWriter) -> None:
    writer.insert_playlist(playlist_id="hard", name="Hard", vendor="local", vendor_pl_id="1")
    org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa", role="remix")
    writer._conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES "
        "('hard', 'tear', 3), ('hard', 'lac-b', 8)"
    )
    stack = works.stack(writer._conn, "lac-a", "hard")
    rows = {m["member_id"]: m for m in stack["members"]}
    assert rows["lac-a"]["this_row"] and not rows["lac-b"]["this_row"]
    assert rows["lac-b"]["place"] == 2, "ranked, not the raw position column"
    assert rows["lac-a"]["place"] is None
    assert rows["lac-a"]["role_label"] == "remix of Lacrimosa"


def test_role_words_never_cross(writer: StateWriter) -> None:
    assert works.role_label("recording", "Requiem") == "recording of Requiem"
    assert works.role_label("live", "Requiem") == "live"
    assert set(works.ROLE_WORDS) == {"original", "recording", "remix", "cover", "edit", "live", "mashup"}


def test_reordering_a_stack_sticks_and_never_touches_a_playlist(writer: StateWriter) -> None:
    writer.insert_playlist(playlist_id="hard", name="Hard", vendor="local", vendor_pl_id="1")
    writer._conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES "
        "('hard', 'lac-a', 1), ('hard', 'lac-b', 2)"
    )
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    org_works.move_members(writer, S, work_id, ["lac-b", "lac-a"])
    assert _members(writer, work_id) == ["lac-b", "lac-a"]
    rows = writer._conn.execute("SELECT stable_id FROM playlist_memberships ORDER BY position").fetchall()
    assert [r[0] for r in rows] == ["lac-a", "lac-b"]


def test_a_stack_order_must_name_every_member(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    with pytest.raises(org.OrgError):
        org_works.move_members(writer, S, work_id, ["lac-a"])


def test_top_track_comes_first_and_a_stale_one_falls_back(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    org.set_work_pref(writer, S, work_id, "lac-b")
    assert _members(writer, work_id)[0] == "lac-b"
    org.set_work_pref(writer, S, work_id, "removed-track")
    assert sorted(_members(writer, work_id)) == ["lac-a", "lac-b"]


def test_unlink_leaves_a_one_member_work_in_place(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    org.unlink_member(writer, S, work_id, "track", "lac-b")
    assert works.overlay(writer._conn)["works"][work_id]["member_count"] == 1


def test_role_change_is_undoable_and_keeps_stack_position(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    org_works.move_members(writer, S, work_id, ["lac-b", "lac-a"])
    org_works.update_member(writer, S, work_id, "track", "lac-a", {"role": "cover"})
    assert _members(writer, work_id) == ["lac-b", "lac-a"]
    org.undo_last(writer, S)
    links = works.overlay(writer._conn)["members"]["lac-a"]
    assert links[0]["role"] == "remix"


# ----- R-WORK-6, E25: rename and merge ------------------------------------------


def test_renaming_onto_an_existing_work_offers_a_merge(writer: StateWriter) -> None:
    keep, other = _work(writer, "Lacrimosa"), _work(writer, "Lacrimosa II")
    with pytest.raises(org.OrgError) as refused:
        org.update_work(writer, S, other, {"title": "LACRIMOSA "})
    assert refused.value.code == "CONFLICT"
    assert refused.value.details["existing_work_id"] == keep


def test_same_title_different_composer_are_different_works(writer: StateWriter) -> None:
    _work(writer, "Ave Maria", "Schubert")
    _work(writer, "Ave Maria", "Gounod")
    assert len(org.list_rows(writer._conn, "work")) == 2


def test_rename_undo_restores_the_title(writer: StateWriter) -> None:
    work_id = _work(writer, "Crazy in Love")
    org.update_work(writer, S, work_id, {"title": "Crazy In Love!"})
    org.undo_last(writer, S)
    assert org.list_rows(writer._conn, "work", {"id": work_id})[0]["title"] == "Crazy in Love"


def test_merge_moves_members_keeps_the_old_name_and_undoes_whole(writer: StateWriter) -> None:
    a = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    b = org_works.link_tracks(writer, S, ["ave-s", "ave-g"], title="Requiem Lacrimosa").row["id"]
    row = org_works.merge_works(writer, S, b, a).row
    assert json.loads(row["aliases"]) == ["Requiem Lacrimosa"]
    assert sorted(_members(writer, a)) == ["ave-g", "ave-s", "lac-a", "lac-b"]
    assert org.list_rows(writer._conn, "work", {"id": b}) == []
    org.undo_last(writer, S)
    assert sorted(_members(writer, b)) == ["ave-g", "ave-s"]
    assert sorted(_members(writer, a)) == ["lac-a", "lac-b"]


def test_merge_keeps_a_primary_when_the_target_already_held_the_track(
    writer: StateWriter,
) -> None:
    source = _work(writer, "Lacrimosa")
    into = _work(writer, "Dies irae")
    org.link_member(writer, S, source, member_kind="track", member_id="tear", role="mashup", is_primary=True)
    org.link_member(writer, S, into, member_kind="track", member_id="tear", role="mashup", is_primary=False)
    org_works.merge_works(writer, S, source, into)
    links = works.overlay(writer._conn)["members"]["tear"]
    assert [(l["work_id"], l["is_primary"]) for l in links] == [(into, True)]


def test_reorder_moves_a_saved_top_track_too(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    org.set_work_pref(writer, S, work_id, "lac-a")
    org_works.move_members(writer, S, work_id, ["lac-b", "lac-a"])
    assert _members(writer, work_id) == ["lac-b", "lac-a"]


def test_a_null_member_field_is_a_validation_error(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    with pytest.raises(org.OrgError) as refused:
        org_works.update_member(writer, S, work_id, "track", "lac-a", {"is_primary": None})
    assert refused.value.code == "VALIDATION"


def test_a_refused_merge_or_undo_changes_nothing(writer: StateWriter) -> None:
    a = _work(writer, "One")
    with pytest.raises(org.OrgError):
        org_works.merge_works(writer, S, a, a)
    assert len(org.list_rows(writer._conn, "work")) == 1


# ----- R-WORK-5, W4, W5, E37, E38: suggestions -----------------------------------


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("Lacrimosa (Leblanc Remix)", "lacrimosa"),
        ("Lacrimosa - ASCO Edit", "lacrimosa"),
        ("Lacrimosa [Extended] feat. Foo", "lacrimosa"),
        ("Sad Clown - Radio Version", "sad clown"),
        ("Song - Part Two", "song part two"),
        ("ロッシーニ (Remix)", "ロッシーニ"),
        ("Чайковский - Dub Mix", "чайковский"),
        ("", ""),
    ],
)
def test_title_stem(title: str, stem: str) -> None:
    assert works.title_stem(title) == stem


def test_suggestions_have_a_reason_and_link_nothing(writer: StateWriter) -> None:
    rows = works.suggest(writer._conn, "lac-a")
    assert [r["member_id"] for r in rows] == ["lac-b"]
    assert rows[0]["reason"] and rows[0]["confidence"] == "low"
    assert org.list_rows(writer._conn, "work_member") == []


def test_title_alone_never_suggests_across_different_composers(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "ave-s", {"composer": "Schubert"})
    org_works.set_classical(writer, S, "ave-g", {"composer": "Gounod"})
    assert works.suggest(writer._conn, "ave-s") == []


def test_composer_known_on_one_side_caps_at_medium(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "ave-s", {"composer": "Schubert"})
    rows = works.suggest(writer._conn, "ave-s")
    assert [(r["member_id"], r["confidence"]) for r in rows] == [("ave-g", "medium")]


def test_same_composer_and_catalogue_is_high_with_different_titles(writer: StateWriter) -> None:
    for track in ("tear", "lac-a"):
        org_works.set_classical(writer, S, track, {"composer": "Mozart", "catalogue": "K. 626"})
    rows = works.suggest(writer._conn, "tear")
    assert rows[0]["member_id"] == "lac-a"
    assert (rows[0]["reason_code"], rows[0]["confidence"]) == ("composer_catalogue", "high")


def test_a_title_containing_a_work_name_suggests_the_work(writer: StateWriter) -> None:
    _track(writer, "dies", "Dies Irae Techno Rework", "Vik")
    work_id = _work(writer, "Dies Irae", None)
    rows = works.suggest(writer._conn, "dies")
    assert [(r["work_id"], r["reason_code"]) for r in rows] == [(work_id, "title_contains_work")]


def test_a_dismissed_pair_stays_dismissed(writer: StateWriter) -> None:
    org.dismiss_pair(writer, S, "lac-b", "lac-a")
    assert works.suggest(writer._conn, "lac-a") == []
    assert works.suggest(writer._conn, "lac-b") == []


def test_a_generic_stem_suggests_nothing(writer: StateWriter) -> None:
    for index in range(works.MAX_STEM_BUCKET + 1):
        _track(writer, f"intro-{index}", "Intro", f"A{index}")
    assert works.suggest(writer._conn, "intro-0") == []


def test_tracks_in_the_same_work_are_not_suggested_again(writer: StateWriter) -> None:
    org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa")
    assert works.suggest(writer._conn, "lac-a") == []


def test_flags_mark_only_tracks_with_a_suggestion(writer: StateWriter) -> None:
    assert works.suggestion_flags(writer._conn, ["lac-a", "tear"]) == ["lac-a"]


def test_link_candidates_list_same_stem_first(writer: StateWriter) -> None:
    rows = works.link_candidates(writer._conn, "lac-a", "")
    assert [r["member_id"] for r in rows] == ["lac-b"]
    searched = works.link_candidates(writer._conn, "lac-a", "a")
    assert searched[0]["member_id"] == "lac-b" and len(searched) > 1


# ----- W7: nothing nags -----------------------------------------------------------


def test_an_unorganized_library_overlay_is_empty(writer: StateWriter) -> None:
    assert works.overlay(writer._conn) == {"works": {}, "members": {}, "classical": {}}


# ----- R-CLS-1, E26: classical metadata ------------------------------------------


def test_classical_round_trips_and_merges(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "tear", {"label": "Deutsche Grammophon", "recording_year": 1962})
    org_works.set_classical(writer, S, "tear", {"conductor": "Karajan", "label": "  DG  "})
    assert org_works.classical_values(writer._conn, "tear") == {
        "label": "DG",
        "recording_year": 1962,
        "conductor": "Karajan",
    }


def test_a_blank_or_null_value_clears_only_that_key(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "tear", {"label": "DG", "conductor": "Karajan"})
    org_works.set_classical(writer, S, "tear", {"label": "", "conductor": None, "ensemble": "WP"})
    assert org_works.classical_values(writer._conn, "tear") == {"ensemble": "WP"}


def test_clearing_every_value_then_undo_restores_them(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "tear", {"label": "DG"})
    org_works.set_classical(writer, S, "tear", {"label": None})
    assert org_works.classical_values(writer._conn, "tear") == {}
    org.undo_last(writer, S)
    assert org_works.classical_values(writer._conn, "tear") == {"label": "DG"}


def test_first_classical_write_undoes_to_nothing_and_rewrites_cleanly(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "tear", {"label": "DG"})
    org.undo_last(writer, S)
    assert org_works.classical_values(writer._conn, "tear") == {}
    org_works.set_classical(writer, S, "tear", {"label": "Sony"})
    assert org_works.classical_values(writer._conn, "tear") == {"label": "Sony"}


def test_a_repeated_classical_write_is_a_no_op(writer: StateWriter) -> None:
    org_works.set_classical(writer, S, "tear", {"label": "DG"})
    assert org_works.set_classical(writer, S, "tear", {"label": "DG"}).undo_id is None


def test_editing_the_conductor_leaves_the_work_alone(writer: StateWriter) -> None:
    work_id = org_works.link_tracks(writer, S, ["lac-a", "lac-b"], title="Lacrimosa").row["id"]
    before = org.list_rows(writer._conn, "work_member", {"work_id": work_id})
    org_works.set_classical(writer, S, "lac-a", {"conductor": "Karajan"})
    assert org.list_rows(writer._conn, "work_member", {"work_id": work_id}) == before


def test_two_recordings_can_share_a_work(writer: StateWriter) -> None:
    work_id = _work(writer)
    for track in ("lac-a", "lac-b"):
        org.link_member(writer, S, work_id, member_kind="track", member_id=track, role="recording", is_primary=True)
    assert sorted(_members(writer, work_id)) == ["lac-a", "lac-b"]


@pytest.mark.parametrize(
    "changes",
    [
        {"recording_year": "1962"},
        {"recording_year": 12},
        {"recording_year": True},
        {"label": 3},
        {"nope": "x"},
    ],
)
def test_bad_classical_values_are_validation_errors(writer: StateWriter, changes: dict[str, object]) -> None:
    with pytest.raises(org.OrgError) as refused:
        org_works.set_classical(writer, S, "tear", changes)
    assert refused.value.code == "VALIDATION"


def test_classical_on_a_missing_track_is_not_found(writer: StateWriter) -> None:
    with pytest.raises(org.OrgError) as refused:
        org_works.set_classical(writer, S, "ghost", {"label": "DG"})
    assert refused.value.code == "NOT_FOUND"
