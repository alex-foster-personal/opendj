"""SYNC-03: RB playlist hierarchy flattener."""
from __future__ import annotations

import pytest

from apps.shared.rekordbox_db import RBPlaylist
from apps.sync.playlist_plan import (
    FOLDER_SEP,
    flatten_rb_playlists,
)


def _pl(id: str, name: str, parent_id: str | None, track_ids: list[str]) -> RBPlaylist:
    return RBPlaylist(id=id, name=name, parent_id=parent_id, track_ids=track_ids)


@pytest.mark.requirement("SYNC-03")
def test_flatten_nested_folders_to_dotted_names() -> None:
    """``Events > 2024 > Berlin`` flattens to ``Events / 2024 / Berlin``."""
    playlists = [
        _pl("1", "Events", None, []),
        _pl("2", "2024", "1", []),
        _pl("3", "Berlin", "2", ["t1", "t2"]),
        _pl("4", "Warmup", None, ["t3"]),
    ]
    flat = flatten_rb_playlists(playlists)
    names = {f.flat_name for f in flat}
    assert "Events / 2024 / Berlin" in names
    assert "Warmup" in names
    # Folder-only nodes (no direct tracks, has children) are dropped.
    assert "Events" not in names
    assert "Events / 2024" not in names


@pytest.mark.requirement("SYNC-03")
def test_folder_only_node_with_no_tracks_is_skipped() -> None:
    """A folder whose only job is to nest other playlists is excluded."""
    playlists = [
        _pl("root", "Genres", None, []),  # folder, no direct tracks
        _pl("leaf", "House", "root", ["t1"]),
    ]
    flat = flatten_rb_playlists(playlists)
    names = {f.flat_name for f in flat}
    assert names == {"Genres / House"}


@pytest.mark.requirement("SYNC-03")
def test_empty_leaf_playlist_is_kept() -> None:
    """A leaf (no children, no tracks) is still an intentional empty playlist."""
    playlists = [_pl("a", "New Empty", None, [])]
    flat = flatten_rb_playlists(playlists)
    assert len(flat) == 1
    assert flat[0].track_ids_ordered == ()
    assert flat[0].flat_name == "New Empty"


@pytest.mark.requirement("SYNC-03")
def test_streaming_only_track_is_excluded() -> None:
    """Streaming tracks are dropped from the flattened membership list."""
    playlists = [_pl("a", "Mixed", None, ["t_local", "t_stream"])]
    flat = flatten_rb_playlists(
        playlists,
        streaming_filter={"t_stream": True, "t_local": False},
    )
    assert flat[0].track_ids_ordered == ("t_local",)


@pytest.mark.requirement("SYNC-03")
def test_streaming_only_playlist_yields_empty_membership() -> None:
    """If every track is streaming, the playlist stays but has no members."""
    playlists = [_pl("a", "Stream Only", None, ["s1", "s2"])]
    flat = flatten_rb_playlists(
        playlists,
        streaming_filter={"s1": True, "s2": True},
    )
    assert flat[0].track_ids_ordered == ()


@pytest.mark.requirement("SYNC-03")
def test_trackno_order_preserved_from_rb_input() -> None:
    """Flattener trusts the input order (iter_playlists already sorts by TrackNo)."""
    playlists = [_pl("a", "Mix", None, ["t3", "t1", "t2"])]  # already sorted upstream
    flat = flatten_rb_playlists(playlists)
    assert flat[0].track_ids_ordered == ("t3", "t1", "t2")


@pytest.mark.requirement("SYNC-03")
def test_empty_input_returns_empty_list() -> None:
    assert flatten_rb_playlists([]) == []


@pytest.mark.requirement("SYNC-03")
def test_folder_sep_constant_is_slash() -> None:
    """The dotted separator matches the spec (" / ")."""
    assert FOLDER_SEP == " / "


@pytest.mark.requirement("SYNC-03")
def test_parent_path_is_the_ancestors() -> None:
    playlists = [
        _pl("1", "Sets", None, []),
        _pl("2", "Peak", "1", ["t1"]),
    ]
    flat = flatten_rb_playlists(playlists)
    peak = next(f for f in flat if f.flat_name == "Sets / Peak")
    assert peak.parent_path == ("Sets",)
