"""RBXAN-8322: the ANALYSE-ME planner decides one write from Open DJ and Rekordbox snapshots.

[if] Open DJ holds a local track Rekordbox lacks or has not analysed [then] the plan adds or queues it exactly once, [else stop].

-Claude
"""

from __future__ import annotations

import unicodedata

import pytest

from apps.rekordbox_analyse.plan import (
    ANALYSE_ME_PLAYLIST,
    VIVA_TARGET_PLAYLIST,
    OdjTrack,
    RbPlaylist,
    RbTrack,
    build_plan,
    playlists_by_name,
)

pytestmark = pytest.mark.requirement("RBXAN-8322")


def _plan(odj, rb=(), playlists=(), **kw):
    return build_plan(odj, rb, playlists, "sha-1", file_exists=lambda _p: True, **kw)


def test_new_local_track_is_added_and_queued():
    plan = _plan([OdjTrack("s1", "/m/a.mp3")])
    assert plan.add_paths == ("/m/a.mp3",), "if a local track missing from Rekordbox is not added then broken"
    assert plan.waiting == 1


def test_unanalysed_rekordbox_track_is_queued_not_re_added():
    plan = _plan([OdjTrack("s1", "/m/a.mp3")], [RbTrack("10", "/m/a.mp3", analysed=False)])
    assert plan.add_paths == ()
    assert plan.playlist_add_ids == ("10",), "if an unanalysed Rekordbox track is not queued then broken"


def test_analysed_track_needs_nothing():
    plan = _plan([OdjTrack("s1", "/m/a.mp3")], [RbTrack("10", "/m/a.mp3", analysed=True)])
    assert plan.is_noop, "if an analysed track produces a write then broken"


def test_analysed_member_is_removed_from_analyse_me():
    plan = _plan(
        [],
        [RbTrack("10", "/m/a.mp3", analysed=True), RbTrack("11", "/m/b.mp3", analysed=False)],
        [RbPlaylist("1", ANALYSE_ME_PLAYLIST, False, ("10", "11"))],
    )
    assert plan.playlist_remove_ids == ("10",), "if an analysed member stays in ANALYSE-ME then broken"
    assert plan.waiting == 1


def test_already_queued_track_is_not_queued_twice():
    plan = _plan(
        [OdjTrack("s1", "/m/a.mp3")],
        [RbTrack("10", "/m/a.mp3", analysed=False)],
        [RbPlaylist("1", ANALYSE_ME_PLAYLIST, False, ("10",))],
    )
    assert plan.is_noop


def test_nfd_and_nfc_paths_match():
    nfd = unicodedata.normalize("NFD", "/m/Beyoncé.mp3")
    plan = _plan([OdjTrack("s1", nfd)], [RbTrack("10", "/m/Beyoncé.mp3", analysed=True)])
    assert plan.is_noop, "if a macOS NFD path is treated as a new track then broken (duplicate in Rekordbox)"


@pytest.mark.parametrize(
    ("path", "exists", "reason"),
    [
        ("spotify:track:abc", True, "streaming"),
        (None, True, "no_file_path"),
        ("/m/a.ogg", True, "unsupported_type"),
        ("/m/gone.mp3", False, "file_missing"),
    ],
)
def test_unwritable_tracks_are_skipped_and_counted(path, exists, reason):
    plan = build_plan([OdjTrack("s1", path)], [], [], "sha", file_exists=lambda _p: exists)
    assert plan.is_noop
    assert plan.skipped == {reason: 1}, f"if a {reason} track is written or not counted then broken"


def test_add_is_capped_and_rest_deferred():
    tracks = [OdjTrack(f"s{i}", f"/m/{i}.mp3") for i in range(7)]
    plan = _plan(tracks, max_add=5)
    assert len(plan.add_paths) == 5 and plan.add_deferred == 2, "if the cap is ignored then broken"


def test_duplicate_open_dj_rows_add_one_track():
    plan = _plan([OdjTrack("s1", "/m/a.mp3"), OdjTrack("s2", "/m/a.mp3")])
    assert plan.add_paths == ("/m/a.mp3",), "if duplicates add the file twice then broken"


def test_viva_tracks_are_filed_in_the_viva_playlist():
    plan = _plan(
        [OdjTrack("s1", "/m/a.mp3", ("Viva - The cut",)), OdjTrack("s2", "/m/b.mp3", ("Viva - The cut",))],
        [RbTrack("10", "/m/b.mp3", analysed=True)],
        [RbPlaylist("2", VIVA_TARGET_PLAYLIST, False, ())],
    )
    assert plan.viva_status == "ok"
    assert plan.viva_add_paths == ("/m/a.mp3",) and plan.viva_add_ids == ("10",)


@pytest.mark.parametrize(
    ("playlists", "status"),
    [((), "missing"), ((RbPlaylist("2", VIVA_TARGET_PLAYLIST, True, ()),), "is_folder")],
)
def test_viva_target_missing_or_folder_is_reported_not_created(playlists, status):
    plan = _plan([OdjTrack("s1", "/m/a.mp3", ("Viva 26",))], [], playlists)
    assert plan.viva_status == status, "if a missing Viva playlist is created or silently skipped then broken"
    assert plan.viva_add_paths == () and plan.add_paths == ("/m/a.mp3",)


def test_analyse_me_folder_is_refused():
    with pytest.raises(ValueError, match="folder"):
        _plan([], [], [RbPlaylist("1", ANALYSE_ME_PLAYLIST, True, ())])


def test_plan_sha_changes_with_content():
    a = _plan([OdjTrack("s1", "/m/a.mp3")])
    b = _plan([OdjTrack("s1", "/m/b.mp3")])
    assert a.plan_sha != b.plan_sha and a.plan_sha == _plan([OdjTrack("s1", "/m/a.mp3")]).plan_sha


def test_playlists_by_name_indexes_and_last_duplicate_wins():
    """[if] duplicate Rekordbox playlist names exist [then] the last row wins in the index, [else stop]."""
    pls = (
        RbPlaylist("1", "dup", False, ()),
        RbPlaylist("2", "dup", False, ("10",)),
        RbPlaylist("3", "solo", False, ("11",)),
    )
    by_name = playlists_by_name(pls)
    assert by_name["dup"].playlist_id == "2"
    assert by_name["solo"].member_ids == ("11",)
