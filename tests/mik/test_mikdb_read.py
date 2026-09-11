"""MIK store reader tests: units, rejections, and the read-only guarantee.

[if] a MIK song/energy row is well-formed or malformed [then] reader normalises/raises, [else stop].
"""
from __future__ import annotations

import sqlite3
from itertools import pairwise
from pathlib import Path

import pytest

from apps.mik import mikdb

pytestmark = pytest.mark.requirement("META-01")


def test_seconds_become_milliseconds(make_mik_store) -> None:
    """The whole point of the series table: ms, not seconds (60.4s -> 60400ms)."""
    store = make_mik_store(
        [
            {
                "name": "5 - A Track",
                "path": "/Users/user/a.mp3",
                "confidence": 0.9,
                "segments": [(0.0, 60.4, 5), (60.4, 14.9, 8)],
            }
        ]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    segments = songs[0].segments
    assert [(s.start_ms, s.length_ms, s.energy) for s in segments] == [
        (0, 60400, 5),
        (60400, 14900, 8),
    ]
    assert [s.seq for s in segments] == [0, 1]
    assert stats.segments_read == 2
    assert stats.segments_rejected == 0


def test_segments_are_ordered_by_start_and_sequenced(make_mik_store) -> None:
    store = make_mik_store(
        [
            {
                "path": "/Users/user/a.mp3",
                "confidence": 0.9,
                "segments": [(30.0, 10.0, 7), (0.0, 30.0, 4), (40.0, 5.0, 9)],
            }
        ]
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(store))
    assert [(s.seq, s.start_ms) for s in songs[0].segments] == [
        (0, 0),
        (1, 30000),
        (2, 40000),
    ]


def test_boundaries_are_rounded_not_durations(make_mik_store) -> None:
    """THE corruption this guards: round(a) + round(b) != round(a + b).

    Rounding ZSTARTTIME and ZLENGTH independently made 3,545 of 7,019 real
    tracks (50.5%) overlap by up to 1 ms, even though MIK has zero overlaps in
    seconds. Measured Tue 28 Jul 2026. These values reproduce it in miniature:
    round(0.0015*1000)=2 and round(0.0015*1000)=2 gives 0..2 and 2..4, but the
    true boundaries are 0.0015 -> 2 and 0.003 -> 3, so the naive length is 2 ms
    where it must be 1 ms.
    """
    store = make_mik_store(
        [
            {
                "path": "/Users/user/a.mp3",
                "confidence": 0.9,
                "segments": [(0.0015, 0.0015, 5), (0.003, 0.0015, 6)],
            }
        ]
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(store))
    spans = [(s.start_ms, s.length_ms) for s in songs[0].segments]
    assert spans == [(2, 1), (3, 2)]
    # The invariant that matters: no segment ends after the next one starts.
    for first, second in pairwise(songs[0].segments):
        assert first.start_ms + first.length_ms <= second.start_ms


def test_no_overlap_across_a_long_synthetic_series(make_mik_store) -> None:
    """Same guard at scale: every boundary lands on a shared millisecond."""
    segments = []
    cursor = 0.0
    for index in range(200):
        length = 0.4995 + (index % 7) * 0.0007
        segments.append((cursor, length, 1 + index % 9))
        cursor += length
    store = make_mik_store(
        [{"path": "/Users/user/a.mp3", "confidence": 0.9, "segments": segments}]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    got = songs[0].segments
    assert len(got) == 200
    assert stats.segments_rejected == 0
    overlaps = [
        (a.seq, b.seq)
        for a, b in pairwise(got)
        if a.start_ms + a.length_ms > b.start_ms
    ]
    assert overlaps == []
    # Contiguous, so each segment ends exactly where the next begins.
    for a, b in pairwise(got):
        assert a.start_ms + a.length_ms == b.start_ms


def test_sub_frame_negative_start_is_clamped_and_counted(make_mik_store) -> None:
    """OBSERVED: 77 real segments start at -0.0195s. Clamp, but never silently."""
    store = make_mik_store(
        [
            {
                "path": "/Users/user/a.mp3",
                "confidence": 0.9,
                "segments": [(-0.0195, 60.0, 5)],
            }
        ]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    segment = songs[0].segments[0]
    assert segment.start_ms == 0
    assert segment.start_clamped is True
    # Length is recomputed from the END boundary, so clamping the start does
    # not silently shift the segment's end.
    assert segment.length_ms == round((-0.0195 + 60.0) * 1000)
    assert stats.segments_clamped_negative_start == 1
    assert stats.segments_rejected == 0


def test_wildly_negative_start_is_rejected_not_clamped(make_mik_store) -> None:
    store = make_mik_store(
        [
            {
                "path": "/Users/user/a.mp3",
                "confidence": 0.9,
                "segments": [(-30.0, 60.0, 5)],
            }
        ]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    assert songs[0].segments == ()
    assert stats.segments_rejected == 1
    assert stats.segments_clamped_negative_start == 0


def test_placeholder_key_and_zero_bpm_are_rejected(make_mik_store) -> None:
    store = make_mik_store(
        [{"path": "/Users/user/a.mp3", "key": "0", "tempo": 0.0, "confidence": 0.9}]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    assert songs[0].key_camelot is None
    assert songs[0].bpm is None
    assert stats.key_rejected == 1
    assert stats.bpm_rejected == 1


def test_energy_outside_the_scale_is_rejected(make_mik_store) -> None:
    store = make_mik_store(
        [{"path": "/Users/user/a.mp3", "energy": 17.0, "confidence": 0.9}]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    assert songs[0].energy is None
    assert stats.energy_rejected == 1


def test_multi_keysegment_store_raises(make_mik_store) -> None:
    """Flow's store has key TIMELINES; ZCONFIDENCE is then not a track scalar."""
    store = make_mik_store([{"path": "/Users/user/a.mp3", "confidence": 0.9}])
    conn = sqlite3.connect(store)
    conn.execute("INSERT INTO ZKEYSEGMENT(ZSONG, ZCONFIDENCE, ZKEY) VALUES (1, 0.5, '9A')")
    conn.commit()
    conn.close()
    with pytest.raises(mikdb.MikReadError, match="more than one ZKEYSEGMENT"):
        mikdb.read_songs(mikdb.open_ro(store))


def test_missing_table_raises_with_the_upgrade_hint(make_mik_store) -> None:
    store = make_mik_store([{"path": "/Users/user/a.mp3"}])
    conn = sqlite3.connect(store)
    conn.execute("DROP TABLE ZENERGYSEGMENT")
    conn.commit()
    conn.close()
    with pytest.raises(mikdb.MikReadError, match="ZENERGYSEGMENT"):
        mikdb.read_songs(mikdb.open_ro(store))


def test_open_ro_refuses_writes(make_mik_store) -> None:
    """If any MIK import path opens a .mikdb writable, that is broken."""
    store = make_mik_store([{"path": "/Users/user/a.mp3"}])
    conn = mikdb.open_ro(store)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE ZSONG SET ZENERGY = 1")


def test_open_ro_missing_store_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mikdb.open_ro(tmp_path / "nope.mikdb")


def test_missing_bookmark_is_counted_not_fatal(make_mik_store) -> None:
    store = make_mik_store([{"path": None, "name": "no path", "confidence": 0.5}])
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    assert songs[0].path is None
    assert songs[0].path_error == "no ZBOOKMARKDATA"
    assert stats.bookmark_missing == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("7 - Control (Extended Mix)", "Control (Extended Mix)"),
        ("10 - Track", "Track"),
        ("Track", "Track"),
        ("1979 - Remaster", "1979 - Remaster"),  # 4 digits: a real title
        ("7 Rings", "7 Rings"),  # no separator: not a prefix
        (None, None),
    ],
)
def test_strip_energy_prefix(raw: str | None, expected: str | None) -> None:
    assert mikdb.strip_energy_prefix(raw) == expected


def test_core_data_date_conversion() -> None:
    # 0 seconds since the Core Data epoch is 2001-01-01T00:00:00Z.
    assert mikdb.core_data_date_to_iso(0.0) == "2001-01-01T00:00:00+00:00"
    assert mikdb.core_data_date_to_iso(None) is None


def test_zero_clipped_peaks_is_a_real_measurement_not_missing(
    make_mik_store,
) -> None:
    """4,647 of 7,026 real rows are 0, meaning "no clipped peaks".

    Treating 0 as a not-analysed sentinel would silently drop those 4,647
    genuine measurements and leave a table implying we only know about clipped
    tracks.
    """
    store = make_mik_store(
        [
            {"path": "/Users/user/a.mp3", "clipped_peaks": 0, "confidence": 0.9},
            {"path": "/Users/user/b.mp3", "clipped_peaks": 88680, "confidence": 0.9},
            {"path": "/Users/user/c.mp3", "clipped_peaks": None, "confidence": 0.9},
        ]
    )
    songs, stats = mikdb.read_songs(mikdb.open_ro(store))
    assert [song.clipped_peak_count for song in songs] == [0, 88680, None]
    assert stats.clipped_peak_count_rejected == 1  # only the genuine NULL


def test_zero_loudness_and_zero_energy_are_not_treated_as_missing(
    make_mik_store,
) -> None:
    """Same falsy-is-not-absent rule for the other numeric scalars."""
    store = make_mik_store(
        [{"path": "/Users/user/a.mp3", "volume": 0.0, "confidence": 0.0}]
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(store))
    assert songs[0].loudness == 0.0
    assert songs[0].key_confidence == 0.0
