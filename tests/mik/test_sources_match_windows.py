"""Sources-map matching on Windows paths, checked portably (MIKBF-01).

The core takes path STRINGS, so a Windows batch is checked on any machine by
feeding it Windows spellings. Mixed In Key on Windows records
``C:\\...\\mik-batch-...\\x.mp3``; the rows and the source lines use the same
spelling.

- [if] a Windows song sits directly in the Windows batch folder [then] it is in the batch, [else stop].
- [if] a Windows song sits in a subfolder or another folder [then] it is ignored and counted, [else stop].
- [if] a Windows source line equals a row below the library root and the row's home [then] it resolves, [else stop].
- [if] a Windows path is spelled decomposed [then] it still binds (NFC is kept), [else stop].
"""

from __future__ import annotations

import dataclasses
import unicodedata

import pytest

from apps.mik import backfill
from apps.mik import sources_match as sources
from apps.mik.mikdb import MikSong

pytestmark = pytest.mark.requirement("MIKBF-01")

BATCH = r"C:\Users\dev\Music\_ingest\mik-batch-20261013-1200"
ROOT = r"D:\air-library"
SID = "a" * 40


def _song(row_id: int, path: str) -> MikSong:
    return MikSong(row_id, "t", "a", None, path, None, None, None, 5, None, None, None, "2026-10-13T00:00:00Z")


def _rows(*pairs: tuple[str, str], platform: str = "win32") -> sources.RowIndex:
    index = sources.RowIndex(platform, {}, {})
    for stable_id, path in pairs:
        sources._index_path(index, stable_id, path)
    return index


def test_a_windows_song_directly_in_the_windows_batch_folder_is_in_the_batch() -> None:
    songs = [
        _song(1, BATCH + r"\x.mp3"),
        _song(2, BATCH + r"\sub\y.mp3"),
        _song(3, r"C:\Users\dev\Music\elsewhere\z.mp3"),
    ]
    result = sources.bind_by_sources(songs, [], _rows(), batch_folder=BATCH, library_root=None)
    assert [song.row_id for song in result.songs] == [1]
    assert result.ignored_outside_batch == 2


def test_a_windows_source_line_binds_below_the_root_and_the_row_home() -> None:
    rows = _rows((SID, r"C:\Users\old\Music\House\x.mp3"))
    result = sources.bind_by_sources(
        [_song(1, BATCH + r"\x.mp3")],
        [ROOT + r"\Music\House\x.mp3"],
        rows,
        batch_folder=BATCH,
        library_root=ROOT,
    )
    assert result.report.matches[1].stable_id == SID, result.details


def test_a_decomposed_windows_name_still_binds() -> None:
    name = unicodedata.normalize("NFD", "Quévardine.mp3")
    rows = _rows((SID, r"C:\Users\old\Music\Quévardine.mp3"))
    result = sources.bind_by_sources(
        [_song(1, BATCH + "\\" + name)],
        [ROOT + "\\Music\\" + name],
        rows,
        batch_folder=BATCH,
        library_root=ROOT,
    )
    assert result.report.matches[1].stable_id == SID, result.details


def test_the_planner_finds_a_root_for_a_windows_source() -> None:
    roots = backfill._roots_of(ROOT + r"\Music\House\x.mp3", (r"C:\Users\old\Music\House\x.mp3",), "linux")
    assert roots == {"D:/air-library"}


def test_posix_paths_still_bind_unchanged() -> None:
    """Control: the macOS spelling the matcher was built for keeps working."""
    rows = _rows((SID, "/Users/old/Music/House/x.mp3"), platform="linux")
    batch = "/Users/dev/Music/_ingest/mik-batch-20261013-1200"
    result = sources.bind_by_sources(
        [_song(1, batch + "/x.mp3"), _song(2, batch + "/sub/y.mp3")],
        ["/Volumes/Air/Music/House/x.mp3"],
        rows,
        batch_folder=batch,
        library_root="/Volumes/Air/",
    )
    assert result.report.matches[1].stable_id == SID and result.ignored_outside_batch == 1


# ---------------------------------------------------------------- case
#
# The platform is an explicit input, so every case rule is checked on any
# host: win32 and darwin default volumes ignore case, Linux does not.


def _bind_case_pair(platform: str, *, batch: str, song: str, line: str, root: str, row: str) -> sources.SourcesMatch:
    return sources.bind_by_sources(
        [_song(1, song)], [line], _rows((SID, row), platform=platform), batch_folder=batch, library_root=root
    )


WINDOWS_CASE = {
    "batch": BATCH,
    "song": r"c:\users\dev\music\_ingest\MIK-BATCH-20261013-1200\track.mp3",
    "line": r"D:\Air-Library\Music\House\Track.mp3",
    "root": r"d:\air-library",
    "row": r"C:\Users\old\music\house\TRACK.mp3",
}
MAC_CASE = {
    "batch": "/Users/dev/Music/_ingest/mik-batch-20261013-1200",
    "song": "/users/dev/music/_ingest/MIK-BATCH-20261013-1200/track.mp3",
    "line": "/Volumes/Air/Music/House/Track.mp3",
    "root": "/volumes/air",
    "row": "/Users/old/music/house/TRACK.mp3",
}


def test_a_windows_case_pair_binds_on_win32() -> None:
    result = _bind_case_pair("win32", **WINDOWS_CASE)
    assert result.report.matches[1].stable_id == SID, result.details


def test_a_mac_case_pair_binds_on_darwin() -> None:
    result = _bind_case_pair("darwin", **MAC_CASE)
    assert result.report.matches[1].stable_id == SID, result.details


def test_the_same_case_pair_does_not_bind_on_linux() -> None:
    """Control: on Linux two spellings are two files, so nothing may bind."""
    result = _bind_case_pair("linux", **MAC_CASE)
    assert result.report.matches == {} and result.ignored_outside_batch == 1


def test_root_derivation_follows_the_platform_case_rule() -> None:
    source, stored = "/Volumes/Air/Music/House/Track.mp3", ("/Users/old/music/house/TRACK.mp3",)
    assert backfill._roots_of(source, stored, "darwin") == {"/volumes/air"}
    assert backfill._roots_of(source, stored, "linux") == set()


def test_one_key_serves_copy_names_and_paths() -> None:
    """Copy names in a flat batch collide by the same rule as paths: case only where the volume ignores it."""
    assert sources.path_key("Track.mp3", platform="darwin") == sources.path_key("track.mp3", platform="darwin")
    assert sources.path_key("Track.mp3", platform="linux") != sources.path_key("track.mp3", platform="linux")


def _two_analyses_of_one_copy(second_energy: int) -> sources.SourcesMatch:
    rows = _rows((SID, r"C:\Users\old\Music\House\x.mp3"))
    first = _song(1, BATCH + r"\x.mp3")
    second = dataclasses.replace(_song(2, BATCH + r"\x.mp3"), energy=second_energy)
    return sources.bind_by_sources(
        [second, first], [ROOT + r"\Music\House\x.mp3"], rows, batch_folder=BATCH, library_root=ROOT
    )


def test_two_disagreeing_analyses_of_one_copy_bind_neither() -> None:
    """if the first of two disagreeing analyses of one copy is still written (row-id order decides) then broken."""
    disagree = _two_analyses_of_one_copy(second_energy=7)
    assert disagree.report.matches == {}
    assert {row.reason for row in disagree.report.unmatched.values()} == {"lost_collision"}
    agree = _two_analyses_of_one_copy(second_energy=5)  # control: agreeing duplicates still bind once
    assert [match.stable_id for match in agree.report.matches.values()] == [SID]
    assert list(agree.report.matches) == [1]
