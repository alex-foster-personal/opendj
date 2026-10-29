"""Duplicate copies of one recording share MIK energy (MIKBF-02).

- [if] every candidate row of an ambiguous song is the same recording [then] each gets its scalar and series, [else stop].
- [if] the candidates are not provably one recording [then] the song stays staged and nothing is shared, [else stop].
- [if] two songs propose different energies for one row [then] that row is written by neither and reported, [else stop].
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from apps.mik import cli, mikdb
from apps.mik import duplicates as dups
from apps.mik import load as loader
from apps.mik import match as matcher
from apps.mik.load_types import LoadError
from apps.shared.equivalence import EquivalenceGate

pytestmark = pytest.mark.requirement("MIKBF-02")

A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40
SEGMENTS = [(0.0, 60.0, 5), (60.0, 30.0, 8)]


def _set(conn: sqlite3.Connection, stable_id: str, **columns: object) -> None:
    for column, value in columns.items():
        conn.execute(f"UPDATE tracks SET {column} = ? WHERE stable_id = ?", (value, stable_id))


def _read(store: Path) -> list[mikdb.MikSong]:
    conn = mikdb.open_ro(store)
    try:
        return mikdb.read_songs(conn)[0]
    finally:
        conn.close()


def _share(conn: sqlite3.Connection, store: Path, *, by_metadata: bool):
    songs = _read(store)
    index = matcher.TrackIndex.from_conn(conn)
    report = matcher.match_songs(songs, index)
    groups = dups.duplicate_groups(conn, by_metadata=by_metadata)
    share = dups.plan_sharing(songs, report, groups, candidates_of=dups.tier_candidates(index))
    return songs, report, share


# ------------------------------------------------------------------ groups


def test_identity_hash_groups_are_on_without_the_flag(state_conn, add_track) -> None:
    """The app's own same-recording rule: equal content or audio hash."""
    add_track(A, file_path="/Users/old/one/x.mp3")
    add_track(B, file_path="/Users/old/two/x.mp3")
    add_track(C, file_path="/Users/old/three/x.mp3")
    _set(state_conn, A, audio_hash="h1")
    _set(state_conn, B, audio_hash="h1")
    groups = dups.duplicate_groups(state_conn, by_metadata=False)
    assert groups.members_of(A) == (A, B)
    assert groups.members_of(C) == (C,)
    assert groups.by_basis() == {"identity_hash": 1}


def test_rows_resolving_to_one_file_are_one_recording_without_the_flag(state_conn, add_track) -> None:
    for stable_id, folder in ((A, "one"), (B, "two"), (C, "three")):
        add_track(stable_id, file_path=f"/Users/old/{folder}/x.mp3")
    for stable_id, checked in ((A, "/Volumes/lib/x.mp3"), (B, "/Volumes/lib/x.mp3"), (C, "/Volumes/lib/y.mp3")):
        state_conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
            "VALUES (?, 'present', ?, '2026-10-13T00:00:00+00:00')",
            (stable_id, checked),
        )
    groups = dups.duplicate_groups(state_conn, by_metadata=False)
    assert groups.members_of(A) == (A, B)
    assert groups.members_of(C) == (C,)
    assert groups.by_basis() == {"same_file": 1}


@pytest.mark.requirement("MIKBF-09")
def test_rows_storing_one_path_are_one_recording_whatever_their_availability(state_conn, add_track) -> None:
    """if two live rows recording the identical file_path stay apart because one is absent then broken."""
    add_track(A, file_path="/Users/old/Music/widerberg/Glasswing/02 Glasswing.mp3")
    add_track(B, file_path="/Users/old/Music/widerberg/Glasswing/02 Glasswing.mp3")
    add_track(C, file_path="/Users/old/Music/widerberg/Glasswing/03 Other.mp3")
    _present(state_conn, A, "/Volumes/lib/02 Glasswing.mp3")  # B has no availability row at all
    groups = dups.duplicate_groups(state_conn, by_metadata=False)
    assert groups.members_of(A) == (A, B)
    assert groups.members_of(C) == (C,)  # control: another stored path stays apart
    assert groups.by_basis() == {"same_stored_path": 1}


@pytest.mark.requirement("MIKBF-09")
def test_stored_paths_differing_only_in_case_never_group(state_conn, add_track) -> None:
    """if two stored spellings that differ only in case group as one recording (a guessed case rule) then broken."""
    add_track(A, file_path="/Users/old/Music/Track.mp3")
    add_track(B, file_path="/Users/old/music/track.mp3")
    add_track(C, file_path="/Users/old/Music/Track.mp3")
    groups = dups.duplicate_groups(state_conn, by_metadata=False)
    assert groups.members_of(A) == (A, C)  # control: the identical spelling does group
    assert groups.members_of(B) == (B,)


def test_metadata_groups_need_the_flag_and_durations_within_one_second(
    state_conn, add_track
) -> None:
    for stable_id, folder in ((A, "one"), (B, "two"), (C, "three"), (D, "four")):
        add_track(stable_id, title="Tamborel ", artist="quilla ventrosk", file_path=f"/Users/old/{folder}/x.mp3")
    _set(state_conn, A, duration_ms=200_000)
    _set(state_conn, B, duration_ms=200_900)
    _set(state_conn, C, duration_ms=201_100)  # more than 1 s from A: its own chunk
    # D has no duration: never grouped
    assert dups.duplicate_groups(state_conn, by_metadata=False).by_basis() == {}
    groups = dups.duplicate_groups(state_conn, by_metadata=True)
    assert groups.members_of(A) == (A, B)
    assert groups.members_of(C) == (C,)
    assert groups.members_of(D) == (D,)
    assert groups.by_basis() == {"artist_title_duration": 1}


def test_metadata_groups_need_the_same_artist_and_title(state_conn, add_track) -> None:
    add_track(A, title="Tamborel", artist="Quilla Ventrosk", file_path="/Users/old/one/x.mp3")
    add_track(B, title="Tamborel", artist="Someone Else", file_path="/Users/old/two/x.mp3")
    add_track(C, title=None, artist="Quilla Ventrosk", file_path="/Users/old/three/x.mp3")
    for stable_id in (A, B, C):
        _set(state_conn, stable_id, duration_ms=200_000)
    assert dups.duplicate_groups(state_conn, by_metadata=True).by_basis() == {}


# ----------------------------------------------------------------- sharing


@pytest.fixture
def twins(state_conn, add_track) -> None:
    """Two rows for one recording under one basename: ambiguous to MIK."""
    add_track(A, title="Tamborel", artist="Quilla Ventrosk", file_path="/Users/old/one/x.mp3")
    add_track(B, title="Tamborel", artist="Quilla Ventrosk", file_path="/Users/old/two/x.mp3")
    _set(state_conn, A, duration_ms=200_000)
    _set(state_conn, B, duration_ms=200_400)
    state_conn.commit()


def test_an_ambiguous_song_shares_to_every_same_recording_candidate(
    state_conn, twins, make_mik_store
) -> None:
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    _songs, report, share = _share(state_conn, store, by_metadata=True)
    assert report.unmatched[1].reason == "ambiguous_candidates"
    assert {match.stable_id for match in share.shared[1]} == {A, B}
    assert {match.tier for match in share.shared[1]} == {dups.SHARE_TIER}
    assert share.conflicts == {}
    assert share.counts()["rows_shared"] == 2


def test_without_a_basis_an_ambiguous_song_shares_nothing(
    state_conn, twins, make_mik_store
) -> None:
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    _songs, _report, share = _share(state_conn, store, by_metadata=False)
    assert share.shared == {}
    assert share.counts()["ambiguous_not_one_recording"] == 1


def test_candidates_of_two_recordings_are_not_shared(
    state_conn, twins, add_track, make_mik_store
) -> None:
    add_track(C, title="Other", artist="Other", file_path="/Users/old/three/x.mp3")
    state_conn.commit()
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    _songs, _report, share = _share(state_conn, store, by_metadata=True)
    assert share.shared == {}
    assert share.counts()["ambiguous_not_one_recording"] == 1


def test_conflicting_energies_for_one_row_are_reported_not_written(
    state_conn, twins, make_mik_store
) -> None:
    store = make_mik_store(
        [
            {"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS},
            {"path": "/elsewhere/x.mp3", "energy": 4.0, "segments": SEGMENTS},
        ]
    )
    _songs, _report, share = _share(state_conn, store, by_metadata=True)
    assert share.shared == {}
    assert share.conflicts == {A: (4, 7), B: (4, 7)}
    assert share.counts()["conflicting_rows"] == 2


def test_agreeing_songs_share_once_from_a_deterministic_winner(
    state_conn, twins, make_mik_store
) -> None:
    store = make_mik_store(
        [
            {"path": "/gone/x.mp3", "energy": 7.0, "confidence": 0.5, "segments": SEGMENTS},
            {"path": "/elsewhere/x.mp3", "energy": 7.0, "confidence": 0.9, "segments": SEGMENTS},
        ]
    )
    _songs, _report, share = _share(state_conn, store, by_metadata=True)
    assert set(share.shared) == {2}  # the higher key confidence wins both rows
    assert {match.stable_id for match in share.shared[2]} == {A, B}


def test_a_unique_match_shares_with_its_duplicate_and_keeps_its_own_row(
    state_conn, twins, make_mik_store
) -> None:
    store = make_mik_store([{"path": "/Users/old/one/x.mp3", "energy": 6.0, "segments": SEGMENTS}])
    _songs, report, share = _share(state_conn, store, by_metadata=True)
    assert report.matches[1].stable_id == A
    assert [match.stable_id for match in share.shared[1]] == [B]


def test_build_plan_writes_scalar_and_series_to_each_shared_row(
    state_conn, twins, make_mik_store, data_dir, write_verdicts
) -> None:
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    songs, report, share = _share(state_conn, store, by_metadata=True)
    plan = loader.build_plan(
        songs, report, EquivalenceGate.load(data_dir), state_conn, shared=share.shared
    )
    assert sorted((w.stable_id, w.field_name, w.value, w.tier) for w in plan.field_writes) == [
        (A, "energy", 7, dups.SHARE_TIER),
        (B, "energy", 7, dups.SHARE_TIER),
    ]
    assert sorted(w.stable_id for w in plan.segment_writes) == [A, B]
    # the song's other fields are still staged; its shared fields are not
    assert {s.field_name for s in plan.staged} == set(loader.GATED_FIELDS) - set(dups.SHARED_FIELDS)


def test_load_cli_shares_only_with_the_flag(
    state_conn, twins, make_mik_store, data_dir, write_verdicts, capsys
) -> None:
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    base = ["load", "--json", "--live", "--fields", "energy,energy_segments"]
    base += ["--data-dir", str(data_dir), "--store", str(store)]
    assert cli.main(base) == 0
    plain = json.loads(capsys.readouterr().out)
    assert plain["applied"]["fields_written"] == 0
    assert plain["duplicate_sharing"]["rows_shared"] == 0
    assert cli.main([*base, "--share-across-duplicates"]) == 0
    shared = json.loads(capsys.readouterr().out)
    assert shared["duplicate_sharing"]["rows_shared"] == 2
    assert shared["duplicate_sharing"]["groups_by_basis"] == {"artist_title_duration": 1}
    assert shared["applied"]["fields_written"] == 2
    assert shared["applied"]["segment_tracks"] == 2
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        rows = conn.execute(
            "SELECT stable_id, value_json FROM track_fields "
            "WHERE field_name = 'energy' AND source = 'mik' ORDER BY stable_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(A, "7"), (B, "7")]


def test_case_variants_that_cannot_be_stat_ed_never_group(state_conn, add_track) -> None:
    """if two case spellings whose files cannot be checked group as one recording (a guessed case rule) then broken."""
    for stable_id, folder in ((A, "one"), (B, "two"), (C, "three")):
        add_track(stable_id, file_path=f"/Users/old/{folder}/x.mp3")
    for stable_id, checked in ((A, "/Volumes/Lib/Track.mp3"), (B, "/volumes/lib/track.mp3"), (C, "/Volumes/Lib/Track.mp3")):
        state_conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
            "VALUES (?, 'present', ?, '2026-10-13T00:00:00+00:00')",
            (stable_id, checked),
        )
    groups = dups.duplicate_groups(state_conn, by_metadata=False)
    assert groups.members_of(A) == (A, C)  # control: an identical spelling is one file
    assert groups.members_of(B) == (B,)


def _present(state_conn, stable_id: str, checked: str) -> None:
    state_conn.execute(
        "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
        "VALUES (?, 'present', ?, '2026-10-13T00:00:00+00:00')",
        (stable_id, checked),
    )


def test_case_variants_that_are_two_files_on_disk_never_group(state_conn, add_track, tmp_path) -> None:
    """if two case spellings that a case-sensitive volume resolves to two files group as one recording then broken."""
    upper, lower = tmp_path / "Track.mp3", tmp_path / "track.mp3"
    upper.write_bytes(b"x")
    if lower.exists():
        pytest.skip(f"capability unavailable: {tmp_path} ignores case, so it cannot hold two case-variant files")
    lower.write_bytes(b"y")
    for stable_id, folder in ((A, "one"), (B, "two")):
        add_track(stable_id, file_path=f"/Users/old/{folder}/x.mp3")
    _present(state_conn, A, str(upper))
    _present(state_conn, B, str(lower))
    assert dups.duplicate_groups(state_conn, by_metadata=False).members_of(A) == (A,)


def test_two_paths_to_one_file_on_disk_group_whatever_their_names(state_conn, add_track, tmp_path) -> None:
    """if two paths the filesystem resolves to one file (a hard link) are two recordings then broken."""
    original = tmp_path / "Track.mp3"
    original.write_bytes(b"x")
    linked = tmp_path / "elsewhere.mp3"
    os.link(original, linked)
    other = tmp_path / "other.mp3"
    other.write_bytes(b"y")
    for stable_id, folder in ((A, "one"), (B, "two"), (C, "three")):
        add_track(stable_id, file_path=f"/Users/old/{folder}/x.mp3")
    _present(state_conn, A, str(original))
    _present(state_conn, B, str(linked))
    _present(state_conn, C, str(other))  # control: a different file stays apart
    groups = dups.duplicate_groups(state_conn, by_metadata=False)
    assert groups.members_of(A) == (A, B) and groups.members_of(C) == (C,)

def test_same_energy_with_different_series_is_a_conflict(state_conn, twins, make_mik_store) -> None:
    """if two songs offering one energy but different series share either field then broken."""
    store = make_mik_store(
        [
            {"path": "/gone/x.mp3", "energy": 7.0, "confidence": 0.5, "segments": [(0.0, 60.0, 7)]},
            {"path": "/elsewhere/x.mp3", "energy": 7.0, "confidence": 0.9, "segments": [(0.0, 30.0, 4)]},
        ]
    )
    _songs, _report, share = _share(state_conn, store, by_metadata=True)
    assert share.shared == {}
    assert set(share.conflicts) == {A, B}


@pytest.mark.parametrize("artists_json", ["", "not json", '{"name": "x"}', '["ok", 7]'])
def test_malformed_artists_json_is_refused_naming_the_row(artists_json: str) -> None:
    """if corrupt artists_json is read as "no artist" (silently out of grouping and status) then broken."""
    with pytest.raises(LoadError, match="track sid-1: artists_json"):
        dups.parse_artists("sid-1", artists_json)
    assert dups.parse_artists("sid-1", '["Quilla Ventrosk"]') == ["Quilla Ventrosk"]  # control
    assert dups.parse_artists("sid-1", None) == []


def test_direct_matches_that_disagree_share_nothing_to_the_rest_of_their_group(
    state_conn, add_track, make_mik_store
) -> None:
    """if two songs bound directly to rows of one recording disagree and a third row is still filled from either then broken."""
    for stable_id, folder in ((A, "one"), (B, "two"), (C, "three")):
        add_track(stable_id, file_path=f"/Users/old/{folder}/x.mp3")
        _set(state_conn, stable_id, audio_hash="h1")
    store = make_mik_store(
        [
            {"path": "/Users/old/one/x.mp3", "energy": 7.0, "segments": SEGMENTS},
            {"path": "/Users/old/two/x.mp3", "energy": 4.0, "segments": SEGMENTS},
        ]
    )
    _songs, report, share = _share(state_conn, store, by_metadata=False)
    assert {match.stable_id for match in report.matches.values()} == {A, B}  # each row's own analysis
    assert share.shared == {}
    assert share.conflicts == {C: (4, 7)}


def test_a_song_without_a_series_shares_nothing(state_conn, twins, make_mik_store) -> None:
    """if a MIK value with no series is shared to duplicates (each then needs its series forever) then broken."""
    scalar_only = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0}])
    _songs, _report, share = _share(state_conn, scalar_only, by_metadata=True)
    assert share.shared == {} and share.conflicts == {}
    complete = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}], name="complete.mikdb")
    _songs, _report, share = _share(state_conn, complete, by_metadata=True)  # control
    assert share.shared != {}


def test_a_collision_loser_still_offers_its_result_to_the_duplicates(
    state_conn, add_track, make_mik_store
) -> None:
    """if two songs claiming one track disagree and the winner alone is shared to its duplicate then broken."""
    for stable_id, folder in ((A, "one"), (B, "two")):
        add_track(stable_id, file_path=f"/Users/old/{folder}/x.mp3")
        _set(state_conn, stable_id, audio_hash="h1")
    disagree = make_mik_store(
        [
            {"path": "/Users/old/one/x.mp3", "energy": 7.0, "segments": SEGMENTS},
            {"path": "/Users/old/one/x.mp3", "energy": 4.0, "segments": SEGMENTS},
        ]
    )
    _songs, report, share = _share(state_conn, disagree, by_metadata=False)
    assert [u.claimed_stable_id for u in report.unmatched.values() if u.reason == "lost_collision"] == [A]
    assert share.shared == {} and share.conflicts == {B: (4, 7)}
    agree = make_mik_store(  # control: two agreeing claims still share once
        [
            {"path": "/Users/old/one/x.mp3", "energy": 7.0, "segments": SEGMENTS},
            {"path": "/Users/old/one/x.mp3", "energy": 7.0, "segments": SEGMENTS},
        ],
        name="agree.mikdb",
    )
    _songs, _report, share = _share(state_conn, agree, by_metadata=False)
    assert share.conflicts == {} and [m.stable_id for ms in share.shared.values() for m in ms] == [B]
