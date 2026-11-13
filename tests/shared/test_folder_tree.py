"""Pure folder tree of stored file paths for the Explorer source (issue #9747).

[if] stored paths are grouped by parent folder [then] every folder counts its own and nested tracks, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.folder_tree import build_folder_forest

pytestmark = pytest.mark.requirement("LIBUX-14606")


def _paths(root: Path) -> dict[str, str | None]:
    return {
        "s1": str(root / "House" / "b.mp3"),
        "s2": str(root / "House" / "A.mp3"),
        "s3": str(root / "House" / "Deep" / "c.flac"),
        "s4": str(root / "Techno" / "d.wav"),
        "s5": None,
        "s6": "bare.mp3",
    }


def test_counts_are_recursive_and_direct(tmp_path: Path) -> None:
    """[if] 4 located tracks sit in 3 folders [then] House counts 3 total and 2 direct, Deep 1, [else stop]."""
    forest = build_folder_forest(_paths(tmp_path))
    house = forest.find(str(tmp_path / "House"))
    assert house is not None, "if a folder holding tracks is not found then broken"
    assert (house.track_count(), len(house.tracks)) == (3, 2), "if counts are not recursive plus direct then broken"
    assert forest.find(str(tmp_path / "House" / "Deep")).track_count() == 1
    assert forest.find(str(tmp_path)).track_count() == 4, "if the parent folder does not sum its children then broken"


def test_tracks_list_direct_or_recursive_in_file_name_order(tmp_path: Path) -> None:
    """[if] House is listed [then] direct gives A then b, recursive appends Deep's track, [else stop]."""
    house = build_folder_forest(_paths(tmp_path)).find(str(tmp_path / "House") + "/")
    assert [sid for sid, _ in house.tracks_in_order(recursive=False)] == ["s2", "s1"], "if not case-folded order"
    assert [sid for sid, _ in house.tracks_in_order(recursive=True)] == ["s2", "s1", "s3"], "if recursion skips"


def test_unlocated_tracks_and_unknown_folders(tmp_path: Path) -> None:
    """[if] a track has no path or a bare name, or a folder holds nothing [then] unlocated, and find is None, [else stop]."""
    forest = build_folder_forest(_paths(tmp_path))
    assert forest.unlocated_ids == ["s5", "s6"], "if pathless tracks are dropped or placed then broken"
    assert forest.find(str(tmp_path / "Nope")) is None, "if an empty folder is invented then broken"
    assert forest.find("") is None


def test_windows_paths_split_on_backslashes() -> None:
    """[if] a stored path is C:\\Music\\House\\a.mp3 [then] it nests under C:\\ > Music > House, [else stop]."""
    forest = build_folder_forest({"w1": "C:\\Music\\House\\a.mp3"})
    assert [root.path for root in forest.sorted_roots()] == ["C:\\"], "if the drive is not the root then broken"
    assert forest.find("C:\\Music\\House").track_count() == 1, "if backslash paths do not nest then broken"


def test_never_touches_the_disk(tmp_path: Path) -> None:
    """[if] the stored paths do not exist on disk [then] the tree is still built from the strings alone, [else stop]."""
    missing = tmp_path / "gone"
    forest = build_folder_forest({"x": str(missing / "a.mp3")})
    assert not missing.exists()
    assert forest.find(str(missing)).track_count() == 1, "if the tree depends on files existing then broken"
