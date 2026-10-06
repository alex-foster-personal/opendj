"""ENRICH-02: the enrich card's honest numbers: values from any source, drain states, absent files.

Regression lines:
  - if a rekordbox BPM is not counted as a ready BPM then broken
  - if a rekordbox 0.0 BPM is counted as a ready BPM then broken
  - if a lane behind an unfinished lane reads as running then broken
  - if a paused drain reads as running then broken
  - if off-machine rows are not grouped by folder in the coverage API then broken
  - if the enrich summary recounts instead of passing the API's numbers through then broken

[if] the card's counts are not the API's source-aware counts [then] fail, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.webui.server import ahead_analysis as aa
from apps.webui.server import enrich_sources as es
from apps.webui.server.ahead_analysis_records import library_value_sources
from apps.webui.server.library_playable import absent_folder
from apps.webui.server.routes import enrich as enrich_routes
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("ENRICH-02")

ORDER = [lane for lane, _b in aa.LANE_ORDER]
STAMP = "2026-10-06T00:00:00Z"


def _lanes(**missing: int) -> dict[str, dict[str, Any]]:
    return {lane: {"total": 10, "done": 10 - missing.get(lane, 0), "missing": missing.get(lane, 0),
                   "failed": 0, "unavailable": None} for lane in ORDER}


def _seed_field(state_db: Path, sid: str, field: str, value: object, source: str = "rekordbox") -> None:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, modified_at) VALUES (?,?,?,?,?)",
            (sid, field, json.dumps(value), source, STAMP),
        )
        conn.commit()
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# usable values
#-----------------------------------------------------------------------------
def test_values_from_any_source_count_as_ready() -> None:
    """[if] a track has a library value or only an Open DJ record [then] it is ready, [else stop]."""
    out = es.usable_counts(["a", "b", "c", "d"], {"a": "rekordbox", "b": "rekordbox"}, {"b", "c"})
    assert out == {"denominator": "present", "total": 4, "ready": 3, "none": 1,
                   "by_source": {"rekordbox": 2, "open_dj": 1}}


def test_a_value_for_an_absent_track_is_not_counted() -> None:
    """[if] only an off-this-Mac track has a value [then] nothing present is ready, [else stop]."""
    out = es.usable_counts(["a"], {"gone": "rekordbox"}, {"gone"})
    assert out["ready"] == 0 and out["none"] == 1 and out["by_source"] == {}


def test_library_value_sources_skips_zero_bpm_and_blank_keys(library: Library) -> None:
    """[if] rekordbox stored 0.0 BPM or an empty key [then] it is no value, [else stop]."""
    for sid in ("a", "b", "c"):
        fx.seed_track(library.state_db, sid, f"/x/{sid}.mp3")
    _seed_field(library.state_db, "a", "bpm", 124.0)
    _seed_field(library.state_db, "b", "bpm", 0.0)
    _seed_field(library.state_db, "c", "bpm", 98.5, source="inferred")
    _seed_field(library.state_db, "a", "key", "8A")
    _seed_field(library.state_db, "b", "key", "")
    connect = lambda: sqlite3.connect(library.state_db)  # noqa: E731
    assert library_value_sources(connect, "bpm") == {"a": "rekordbox", "c": "inferred"}
    assert library_value_sources(connect, "key") == {"a": "rekordbox"}


#-----------------------------------------------------------------------------
# drain states
#-----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("state", "missing", "expected"),
    [
        ("paused_playing", {"beatgrid": 3}, {"loudness": "done", "beatgrid": "paused_playing"}),
        ("ran:loudness", {"loudness": 2, "beatgrid": 3}, {"loudness": "running", "beatgrid": "waiting"}),
        ("ran:strip", {"loudness": 2}, {"loudness": "waiting"}),
        ("timeout:plan", {"key": 1}, {"key": "stalled"}),
        ("idle", {"key": 1}, {"key": "starting"}),
        ("green", {}, {"loudness": "done", "key": "done"}),
    ],
    ids=["paused", "waiting-on-lane", "waiting-on-strip", "stalled", "starting", "done"],
)
def test_each_drain_state_is_named(state: str, missing: dict[str, int], expected: dict[str, str]) -> None:
    """[if] the drain is in a given state [then] each lane names it, [else stop]."""
    out = es.lane_drain_states(state, _lanes(**missing), ORDER)
    assert {lane: out[lane]["state"] for lane in expected} == expected
    assert set(out) == set(ORDER)


def test_a_waiting_lane_names_the_lane_it_waits_for() -> None:
    """[if] waveform is unfinished [then] key waits on waveform, not on itself, [else stop]."""
    out = es.lane_drain_states("ran:waveform", _lanes(waveform=5, key=9), ORDER)
    assert out["waveform"] == {"state": "running", "waiting_on": None, "reason": None}
    assert out["key"] == {"state": "waiting", "waiting_on": "waveform", "reason": None}


def test_an_unavailable_lane_never_blocks_later_ones() -> None:
    """[if] loudness cannot run here [then] waveform runs, [else stop]."""
    lanes = _lanes(loudness=4, waveform=4)
    lanes["loudness"]["unavailable"] = "ffmpeg lacks astats"
    out = es.lane_drain_states("ran:waveform", lanes, ORDER)
    assert out["loudness"]["state"] == "unavailable" and out["waveform"]["state"] == "running"


def test_drain_coverage_carries_usable_and_drain_states() -> None:
    """[if] the drain computes coverage [then] BPM and key carry usable counts, [else stop]."""
    sources = aa.AheadSources(
        present_fn=lambda: ["a", "b", "c"],
        mapped_fn=lambda ids: set(ids),
        has_strip_fn=lambda _sid: True,
        write_strip_fn=lambda _sid: None,
        done_fn=lambda lane, _b: {"c"} if lane == "beatgrid" else set(),
        run_lane_fn=lambda _l, _b, _ids: {},
        playing_fn=lambda: True,
        blank_tags_fn=set,
        refresh_tags_fn=lambda _sid: True,
        declined_fn=lambda _l, _b: {},
        library_values_fn=lambda field: {"a": "rekordbox", "b": "rekordbox"} if field == "bpm" else {},
    )
    drain = aa.AheadDrain(sources)
    assert drain.tick() == "paused_playing"
    cov = drain.refresh_coverage()
    assert cov["lanes"]["beatgrid"]["usable"]["by_source"] == {"rekordbox": 2, "open_dj": 1}
    assert cov["lanes"]["key"]["usable"]["none"] == 3
    assert "usable" not in cov["lanes"]["loudness"]
    assert cov["drain"]["beatgrid"]["state"] == "paused_playing"


#-----------------------------------------------------------------------------
# absent files, by folder
#-----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "folder"),
    [
        ("/Users/me/Music/Convert/deemix Music/a.mp3", "~/Music/Convert"),
        ("/Users/me/Music/a.mp3", "~/Music"),
        ("/Users/elsewhere/Documents/TuneFab/x/a.mp3", "/Users/elsewhere/Documents/TuneFab"),
        ("/Volumes/SLATER/crate/a.mp3", "/Volumes/SLATER"),
        ("/contents_815473895/x/a.mp3", "/contents_815473895/x"),
    ],
)
def test_absent_folder_groups_by_two_levels(path: str, folder: str) -> None:
    """[if] a missing file is under a home or a drive [then] it groups two levels down, [else stop]."""
    assert absent_folder(path, "/Users/me") == folder


def test_coverage_api_and_enrich_summary_name_absent_folders(library: Library) -> None:
    """[if] rows point at files not on this Mac [then] both routes name the folders, [else stop]."""
    library.client.app.include_router(enrich_routes.router, prefix="/api/v1")
    fx.seed_track(library.state_db, "here", str(fx.audio_file(library.music, "a.mp3")))
    for i in range(3):
        fx.seed_track(library.state_db, f"g{i}", f"/Users/elsewhere/Music/Convert/set{i}/t.mp3")
    fx.seed_track(library.state_db, "h", "/Users/elsewhere/Documents/TuneFab/t.mp3")
    coverage = library.client.get("/api/v1/ingest/coverage").json()
    assert coverage["availability"]["off_machine"] == 4
    assert coverage["absent_folders"] == [
        {"folder": "/Users/elsewhere/Music/Convert", "tracks": 3},
        {"folder": "/Users/elsewhere/Documents/TuneFab", "tracks": 1},
    ]
    summary = library.client.get("/api/v1/enrich/summary").json()
    assert summary["coverage"]["absent_folders"] == coverage["absent_folders"], (
        "if the card's folders differ from the coverage API's then broken"
    )
    assert summary["coverage"]["availability"] == coverage["availability"]
