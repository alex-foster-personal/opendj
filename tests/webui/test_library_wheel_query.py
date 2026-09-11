"""Acceptance tests for LIBUX-06's library-wheel aggregation query.

Requirements:
✔︎ The real library, grouped by genre family, from state.db + master.plain.db -- never demo nodes.
✔︎ decade / overplayed_ness / set_played_in report disabled with a stated reason; never fake data.
✔︎ play_count / popularity / playlist axes carry a real per-track value and an explanatory title.
✔︎ A missing state.db fails loudly rather than returning an empty success payload.

Acceptance tests:
[if] the wheel is queried against a real library [then ⛔️] any family/track
     is invented rather than read from the DBs
[if] axis=decade|overplayed_ness|set_played_in is requested [then ⛔️] a
     numeric axis_value is fabricated
[if] axis=play_count|popularity|playlist is requested [then ⛔️] a track's
     axis_value or axis_title is missing
[if] state.db does not exist [then ⛔️] the query returns a payload instead of raising
[if] an unsupported axis key is requested [then ⛔️] anything other than ValueError is raised
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.library_wheel.query import LibraryWheelError, query_library_wheel

pytest_plugins = ("tests.webui.library_wheel_fixtures",)


def test_families_and_unclassified_bucket_from_real_data(wheel_dbs: tuple[Path, Path]) -> None:
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis="play_count")

    assert result["schema_version"] == 1
    assert result["total_tracks"] == 5
    # t-unmatched-1 (genre matches no family) + t-no-rb-1 (no rekordbox mapping at all).
    assert result["unclassified_track_count"] == 2

    families_by_name = {f["name"]: f for f in result["families"]}
    assert set(families_by_name) == {"techno", "house"}
    assert families_by_name["techno"]["track_count"] == 2
    assert families_by_name["techno"]["color"] == "#5ec8ff"
    assert families_by_name["house"]["track_count"] == 1

    techno_genres = {g["tag"]: g for g in families_by_name["techno"]["genres"]}
    assert set(techno_genres) == {"Peak Time Techno"}
    stable_ids = {t["stable_id"] for t in techno_genres["Peak Time Techno"]["tracks"]}
    assert stable_ids == {"t-techno-1", "t-techno-2"}


def test_play_count_axis_carries_real_value_and_title(wheel_dbs: tuple[Path, Path]) -> None:
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis="play_count")
    assert result["selected_axis_enabled"] is True
    assert result["selected_axis_reason"] is None

    track = _find_track(result, "t-techno-1")
    assert track["axis_value"] == 40
    assert "40" in track["axis_title"]


def test_popularity_axis_ranks_within_family_not_globally(wheel_dbs: tuple[Path, Path]) -> None:
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis="popularity")
    assert result["selected_axis_enabled"] is True

    hi = _find_track(result, "t-techno-1")  # 40 plays, top of a 2-track family
    lo = _find_track(result, "t-techno-2")  # 10 plays, bottom of the same family
    assert hi["axis_value"] > lo["axis_value"]
    assert "techno" in hi["axis_title"]
    assert "2" in hi["axis_title"]  # denominator: 2 tracks with a resolved family


def test_playlist_axis_counts_real_memberships(wheel_dbs: tuple[Path, Path]) -> None:
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis="playlist")
    in_playlist = _find_track(result, "t-techno-1")
    not_in_playlist = _find_track(result, "t-techno-2")
    assert in_playlist["axis_value"] == 1
    assert not_in_playlist["axis_value"] == 0


def test_soft_deleted_track_is_absent_from_every_wheel_count(
    wheel_dbs: tuple[Path, Path],
) -> None:
    state_db, master_db = wheel_dbs
    with sqlite3.connect(state_db) as state:
        state.execute(
            "UPDATE tracks SET deleted_at = '2026-09-04T00:00:00Z' "
            "WHERE stable_id = 't-techno-1'"
        )

    result = query_library_wheel(state_db, master_db, axis="play_count")

    assert result["total_tracks"] == 4
    assert result["unclassified_track_count"] == 2
    with pytest.raises(AssertionError):
        _find_track(result, "t-techno-1")


def test_popularity_axis_gives_tied_play_counts_the_same_percentile(
    wheel_dbs: tuple[Path, Path],
) -> None:
    state_db, master_db = wheel_dbs
    with sqlite3.connect(master_db) as master:
        master.execute("UPDATE djmdContent SET DJPlayCount = 10 WHERE ID = 'v-1'")

    result = query_library_wheel(state_db, master_db, axis="popularity")

    assert _find_track(result, "t-techno-1")["axis_value"] == 50
    assert _find_track(result, "t-techno-2")["axis_value"] == 50


def test_playlist_axis_counts_distinct_playlists_not_membership_rows(
    wheel_dbs: tuple[Path, Path],
) -> None:
    state_db, master_db = wheel_dbs
    with sqlite3.connect(state_db) as state:
        state.execute(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
            "VALUES ('pl-1', 't-techno-1', 99)"
        )

    result = query_library_wheel(state_db, master_db, axis="playlist")

    assert _find_track(result, "t-techno-1")["axis_value"] == 1


@pytest.mark.parametrize(
    ("axis", "reason_substring"),
    [
        ("decade", "release year"),
        ("overplayed_ness", "popularity-curve"),
        ("set_played_in", "SET-08"),
    ],
)
def test_prerequisite_gapped_axes_report_disabled_not_faked(
    wheel_dbs: tuple[Path, Path], axis: str, reason_substring: str
) -> None:
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis=axis)
    assert result["selected_axis_enabled"] is False
    assert reason_substring in result["selected_axis_reason"]
    # The tree is still real (no demo nodes); only the per-track axis value is withheld.
    assert result["families"]
    track = _find_track(result, "t-techno-1")
    assert track["axis_value"] is None
    assert track["axis_title"] is None

    axis_entry = next(a for a in result["axes"] if a["key"] == axis)
    assert axis_entry["enabled"] is False
    assert reason_substring in axis_entry["reason"]


def test_unsupported_axis_raises_value_error(wheel_dbs: tuple[Path, Path]) -> None:
    state_db, master_db = wheel_dbs
    with pytest.raises(ValueError):
        query_library_wheel(state_db, master_db, axis="not-a-real-axis")


def test_missing_state_db_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(LibraryWheelError):
        query_library_wheel(tmp_path / "missing.db", tmp_path / "missing-master.db")


def _find_track(result: dict, stable_id: str) -> dict:
    for family in result["families"]:
        for genre in family["genres"]:
            for track in genre["tracks"]:
                if track["stable_id"] == stable_id:
                    return track
    raise AssertionError(f"{stable_id} not found in result")
