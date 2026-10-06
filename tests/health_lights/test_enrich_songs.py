"""ENRICH-03: the enrich card counts unique songs, duds are not failures, red only at the share.

Regression lines:
  - if two rows for one file count as two songs then broken
  - if two copies of one song (same title, artists, whole seconds) count as two then broken
  - if a dud (undecodable, untagged, gone) adds to failed then broken
  - if 4.9 % real failures read red, or 5.0 % do not, then broken
  - if the coverage API and the enrich summary disagree on songs then broken

[if] the card counts rows, fails duds or reds under the share [then] fail, [else stop].
"""
from __future__ import annotations

import pytest

from apps.webui.server import ahead_analysis as aa
from apps.webui.server import enrich_songs as es
from apps.webui.server.routes import enrich as enrich_routes
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("ENRICH-03")

DUD = "TrackUnreadable: a.mp3: ffmpeg exited 69: Could not find codec parameters for stream 0"
REAL = "LaneContractError: beat past end"


def _index(*rows: tuple[str, str, str | None, list[str], int | None]) -> es.SongIndex:
    return es.build_song_index(rows)


def _flat(n: int) -> es.SongIndex:
    return _index(*[(f"s{i}", f"/m/{i}.mp3", f"t{i}", ["A"], 200_000) for i in range(n)])


#-----------------------------------------------------------------------------
# dedupe
#-----------------------------------------------------------------------------
def test_two_rows_for_one_file_are_one_song() -> None:
    """[if] two rows share a path, whatever their tags [then] they are one song, [else stop]."""
    index = _index(("a", "/m/x.mp3", "One", ["A"], 200_000), ("b", "/m/x.mp3", "Other", ["B"], 99_000))
    assert (index.songs, index.files, len(index.song_of)) == (1, 1, 2)


def test_copies_of_one_song_are_one_song() -> None:
    """[if] title, artists and whole seconds match after normalizing [then] one song, [else stop]."""
    index = _index(
        ("a", "/m/a.mp3", "Rhythm's All You Need", ["X", "Y"], 241_400),
        ("b", "/m/BACKUP/a.mp3", "  rhythm's all you NEED ", ["y", "x"], 240_600),
        ("c", "/m/c.mp3", "Rhythm's All You Need", ["X", "Y"], 300_000),
    )
    assert index.songs == 2 and index.files == 3


def test_a_row_missing_key_parts_never_merges_on_metadata() -> None:
    """[if] title, artists or duration is missing [then] only its path can join it, [else stop]."""
    index = _index(("a", "/m/a.mp3", "Intro", [], 60_000), ("b", "/m/b.mp3", "Intro", [], 60_000))
    assert index.songs == 2


#-----------------------------------------------------------------------------
# duds and the red share
#-----------------------------------------------------------------------------
def test_a_dud_never_adds_to_failed() -> None:
    """[if] a song's only failures are dud reasons [then] it counts in duds, not failed, [else stop]."""
    out = es.lane_songs(_flat(3), ["s0", "s1", "s2"], {"s0"}, {}, {"s1": DUD, "s2": REAL})
    assert (out["done"], out["failed"], out["duds"], out["missing"]) == (1, 1, 1, 0)
    assert out["failed_reasons"] == {REAL: 1}


def test_a_song_with_any_copy_done_is_done() -> None:
    """[if] one copy of a song is done and another failed [then] the song is done, [else stop]."""
    index = _index(("a", "/m/a.mp3", "T", ["A"], 200_000), ("b", "/m/b.mp3", "T", ["A"], 200_000))
    out = es.lane_songs(index, ["a", "b"], {"a"}, {}, {"b": REAL})
    assert (out["total"], out["done"], out["failed"]) == (1, 1, 0)


@pytest.mark.parametrize(("failed", "red"), [(49, False), (50, True)], ids=["4.9pct-ready", "5.0pct-red"])
def test_red_only_at_the_share(failed: int, red: bool) -> None:
    """[if] real failures are 4.9 % [then] not red; at 5.0 % [then] red, [else stop]."""
    ids = [f"s{i}" for i in range(1000)]
    out = es.lane_songs(_flat(1000), ids, set(ids[failed:]), {}, dict.fromkeys(ids[:failed], REAL))
    assert out["failed"] == failed and out["red"] is red


def test_duds_at_any_share_are_never_red() -> None:
    """[if] half a lane is duds [then] it is still not red, [else stop]."""
    ids = [f"s{i}" for i in range(10)]
    out = es.lane_songs(_flat(10), ids, set(ids[5:]), {}, dict.fromkeys(ids[:5], DUD))
    assert out["duds"] == 5 and out["red"] is False


def test_dud_files_count_files_once_across_lanes() -> None:
    """[if] one file is a dud in two lanes [then] it is one dud file, [else stop]."""
    index = _index(("a", "/m/x.mp3", "T", ["A"], 1000), ("b", "/m/x.mp3", "T", ["A"], 1000))
    out = es.dud_files(index, [{"a": DUD}, {"b": "the file still reads no tags or no duration"}, {"a": REAL}])
    assert out == {"files": 1, "reasons": {"the audio cannot be decoded": 1}}


def test_usable_and_step_songs_fold_copies() -> None:
    """[if] one copy has a rekordbox BPM [then] the song is ready, [else stop]."""
    index = _index(("a", "/m/a.mp3", "T", ["A"], 1000), ("b", "/m/b.mp3", "T", ["A"], 1000), ("c", "/m/c.mp3", None, [], None))
    usable = es.usable_songs(index, ["a", "b", "c"], {"b": "rekordbox"}, set())
    assert (usable["total"], usable["ready"], usable["none"], usable["allowable"]) == (2, 1, 1, False)
    step = es.step_songs(index, ["a", "b", "c"], done={"a"}, terminal=set(), failed={"c"})
    assert step == {"done": 1, "terminal": 0, "pending": 0, "failed": 1, "red": True}


def test_drain_coverage_reports_songs_and_duds() -> None:
    """[if] the drain computes coverage with a song index [then] lanes carry songs, [else stop]."""
    index = _index(("a", "/m/x.mp3", "T", ["A"], 1000), ("b", "/m/x.mp3", "T", ["A"], 1000), ("c", "/m/c.mp3", "U", ["A"], 1000))
    sources = aa.AheadSources(
        present_fn=lambda: ["a", "b", "c"], mapped_fn=set, has_strip_fn=lambda _s: True,
        write_strip_fn=lambda _s: None, done_fn=lambda _l, _b: set(), run_lane_fn=lambda _l, _b, _i: {},
        playing_fn=lambda: True, blank_tags_fn=set, refresh_tags_fn=lambda _s: True, declined_fn=lambda _l, _b: {},
        library_values_fn=lambda field: {"a": "rekordbox"} if field == "bpm" else {},
        song_index_fn=lambda _ids: index,
    )
    cov = aa.AheadDrain(sources).refresh_coverage()
    assert cov["songs"]["songs"] == 2 and cov["songs"]["red_fail_share"] == es.CFG.RED_FAIL_SHARE
    assert cov["lanes"]["loudness"]["songs"]["total"] == 2 and cov["lanes"]["loudness"]["total"] == 3
    assert cov["lanes"]["beatgrid"]["usable_songs"]["ready"] == 1
    assert cov["duds"] == {"files": 0, "reasons": {}}


#-----------------------------------------------------------------------------
# HTTP: the coverage API folds songs, the summary passes them through
#-----------------------------------------------------------------------------
def test_coverage_and_summary_count_one_file_once(library: Library) -> None:
    """[if] two rows point at one file [then] coverage and the summary say one song, [else stop]."""
    library.client.app.include_router(enrich_routes.router, prefix="/api/v1")
    audio = fx.audio_file(library.music, "a.mp3")
    fx.seed_track(library.state_db, "a", str(audio))
    fx.seed_track(library.state_db, "b", str(audio))
    songs = library.client.get("/api/v1/ingest/coverage").json()["songs"]
    assert (songs["songs"], songs["files"], songs["rows"]) == (1, 1, 2)
    assert songs["steps"]["stems"]["pending"] == 1
    summary = library.client.get("/api/v1/enrich/summary").json()
    assert summary["coverage"]["songs"] == songs, "if the card's songs differ from the coverage API's then broken"
    assert (summary["stems"]["separated"], summary["stems"]["not_yet"]) == (0, 1)


@pytest.mark.parametrize(
    ("failed", "pending", "allowable", "red"),
    [(0, 49, True, False), (0, 50, False, False), (49, 2, False, False), (50, 0, False, True)],
    ids=["4.9pct-pending-green", "5.0pct-pending-neutral", "4.9pct-failed-plus-0.2pct-pending-neutral", "5.0pct-failed-red"],
)
def test_failures_plus_pending_decide_green(failed: int, pending: int, allowable: bool, red: bool) -> None:
    """[if] failures plus pending stay under 5 % [then] allowable; red needs failures alone, [else stop]."""
    ids = [f"s{i}" for i in range(1000)]
    done = set(ids[failed + pending:])
    out = es.lane_songs(_flat(1000), ids, done, {}, dict.fromkeys(ids[:failed], REAL))
    assert (out["failed"], out["missing"]) == (failed, pending)
    assert (out["allowable"], out["red"]) == (allowable, red)
