"""rekordbox positions on our timeline: the MP3 lead-in shift at every boundary (NAE-22).

[if] a rekordbox cue, beat or phrase belongs to an MP3 whose encoder lead-in our decoders trim [then] it is served with that lead-in taken off, and written back to rekordbox with it put back, [else stop].

Regression one-liners:
  - if fetch_cues serves rekordbox's raw InMsec for a tagged MP3 then broken
  - if a track with no local file has its cues moved anyway then broken (overshoot)
  - if /anlz serves PQTZ times or phrases unshifted for a tagged MP3 then broken
  - if a beat the shift puts before zero is served with a negative time then broken
  - if a cue list for a tagged MP3 keeps rekordbox's raw times then broken
  - if PQTZ write-back sends our times to rekordbox without the lead-in then broken
  - if PQTZ write-back goes ahead when the lead-in cannot be read then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.shared.mp3_lead_in import cues_on_our_timeline, rekordbox_lead_in_s
from apps.sync.analysis_writeback import _beats_on_rekordbox_timeline
from apps.sync.safety import SafetyAbort
from apps.webui.server import rb_vendor
from apps.webui.server.rb_vendor_pkg.lead_in_shift import on_our_timeline

pytestmark = [pytest.mark.requirement("NAE-22")]

TAGGED = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup" / "src-128.mp3"
#: 1105 samples at 22.05 kHz.
LEAD_IN_S = 1105 / 22_050
TAGGED_ID, MISSING_ID = "11", "12"


@pytest.fixture
def master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "master.plain.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, FolderPath VARCHAR(255))")
        conn.execute(
            "CREATE TABLE djmdCue (ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), "
            "InMsec INTEGER, InFrame INTEGER, InMpegFrame INTEGER, InMpegAbs INTEGER, "
            "OutMsec INTEGER, OutFrame INTEGER, Kind INTEGER, ColorTableIndex INTEGER, "
            "ActiveLoop INTEGER, Comment VARCHAR(255), BeatLoopSize INTEGER, "
            "rb_local_deleted TINYINT(1) DEFAULT 0, updated_at DATETIME)"
        )
        conn.executemany(
            "INSERT INTO djmdContent (ID, FolderPath) VALUES (?, ?)",
            [(TAGGED_ID, str(TAGGED)), (MISSING_ID, str(tmp_path / "not-here.mp3"))],
        )
        for cid in (TAGGED_ID, MISSING_ID):
            conn.execute(
                "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ActiveLoop) "
                "VALUES (?, ?, 1000, -1, 1, 0)",
                (f"hot-{cid}", cid),
            )
            conn.execute(
                "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ActiveLoop) "
                "VALUES (?, ?, 4000, 8000, 0, 0)",
                (f"loop-{cid}", cid),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", path)
    return path


def _positions(cues: list[dict[str, Any]]) -> list[tuple[int | None, int | None]]:
    return [(c["in_ms"], c["out_ms"]) for c in cues]


def test_fetch_cues_takes_the_lead_in_off_a_tagged_mp3(master: Path) -> None:
    assert rekordbox_lead_in_s(str(TAGGED)) == pytest.approx(LEAD_IN_S)
    assert _positions(rb_vendor.fetch_cues(TAGGED_ID)) == [(950, None), (3950, 7950)]


def test_fetch_cues_leaves_a_track_with_no_local_file_as_stored(master: Path) -> None:
    assert _positions(rb_vendor.fetch_cues(MISSING_ID)) == [(1000, None), (4000, 8000)]


def _anlz_payload() -> dict[str, Any]:
    return {
        "stable_id": "s",
        "beatgrid": {
            "source": "rekordbox",
            "beat_count": 3,
            "beats": [
                {"n": 1, "bpm": 120.0, "t": 0.02},
                {"n": 2, "bpm": 120.0, "t": 0.52},
                {"n": 3, "bpm": 120.0, "t": 1.02},
            ],
        },
        "phrases": [{"start_s": 0.02, "end_s": 1.02, "kind": 1, "mood": 1}],
    }


def test_anlz_grid_and_phrases_move_onto_our_timeline() -> None:
    raw = _anlz_payload()
    shifted = on_our_timeline(raw, LEAD_IN_S)
    grid = shifted["beatgrid"]
    # The 20 ms beat lies inside the 50 ms lead-in: no engine of ours plays it.
    assert [b["t"] for b in grid["beats"]] == [0.47, 0.97]
    assert [b["n"] for b in grid["beats"]] == [2, 3]
    assert grid["beat_count"] == 2
    assert shifted["phrases"] == [{"start_s": 0.0, "end_s": 0.97, "kind": 1, "mood": 1}]
    assert raw == _anlz_payload(), "the cached payload must not be mutated"


def test_anlz_payload_is_untouched_without_a_lead_in() -> None:
    raw = _anlz_payload()
    assert on_our_timeline(raw, 0.0) is raw


def test_cue_lists_move_onto_our_timeline() -> None:
    cues: list[dict[str, Any]] = [
        {"kind": "hot_cue", "slot": "A", "in_ms": 1000, "out_ms": None},
        {"kind": "loop", "slot": None, "in_ms": 4000, "out_ms": 8000},
    ]
    assert _positions(cues_on_our_timeline(cues, str(TAGGED))) == [
        (950, None),
        (3950, 7950),
    ]
    assert cues_on_our_timeline(cues, "/nowhere/x.mp3") == cues


def test_pqtz_write_back_puts_the_lead_in_back(master: Path) -> None:
    conn = sqlite3.connect(str(master))
    try:
        ours: list[dict[str, object]] = [{"n": 1, "bpm": 120.0, "t": 0.47}, {"n": 2, "bpm": 120.0, "t": 0.97}]
        back = _beats_on_rekordbox_timeline(conn, TAGGED_ID, ours)
        assert [b["t"] for b in back] == pytest.approx([0.47 + LEAD_IN_S, 0.97 + LEAD_IN_S])
        # Round trip: shifting back out lands on what /anlz served.
        served = on_our_timeline(
            {"beatgrid": {"beats": back}, "phrases": []}, LEAD_IN_S
        )["beatgrid"]["beats"]
        assert [b["t"] for b in served] == [0.47, 0.97]
        with pytest.raises(SafetyAbort, match="lead-in"):
            _beats_on_rekordbox_timeline(conn, MISSING_ID, ours)
    finally:
        conn.close()


class _FakePqtz:
    def get_beats(self) -> list[int]:
        return [1, 2, 3]

    def get_bpms(self) -> list[float]:
        return [120.0, 120.0, 120.0]

    def get_times(self) -> list[float]:
        return [0.02, 0.52, 1.02]


@pytest.mark.parametrize("cache_hit", [True, False], ids=["cache-hit", "fresh-parse"])
def test_build_anlz_payload_serves_the_grid_on_our_timeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_hit: bool
) -> None:
    """Both branches of the real builder shift, and the cache keeps rekordbox's numbers."""
    from apps.adapters.rekordbox import paths as rb_paths
    from apps.shared import platform_paths as pp
    from apps.webui.server.rb_vendor_pkg import anlz as rb_anlz
    from apps.webui.server.rb_vendor_pkg import anlz_cache as rb_anlz_cache
    from apps.webui.server.rb_vendor_pkg import db as rb_db

    content = rb_vendor.RbContent(
        stable_id="track-1",
        vendor_id="vendor-1",
        folder_path=str(TAGGED),
        image_path=None,
        analysis_data_path="ANLZ0000.DAT",
        length_s=None,
        comment=None,
        genre=None,
    )
    stored: list[dict[str, Any]] = []
    monkeypatch.setattr(rb_paths, "anlz_dir", lambda _content: tmp_path)
    real_resolve = rb_paths.resolve_asset_path
    monkeypatch.setattr(
        rb_paths,
        "resolve_asset_path",
        lambda p, **kw: pp.MappedPath(
            original=p, resolved=tmp_path / "ANLZ0000.DAT", mapped=True, reason="native"
        )
        if p == "ANLZ0000.DAT"
        else real_resolve(p, **kw),
    )
    monkeypatch.setattr(rb_anlz_cache, "_anlz_mtime", lambda _directory: 1.0)
    monkeypatch.setattr(
        rb_anlz_cache,
        "_load_cached_payload",
        lambda *_a: {**_anlz_payload(), "waveform": {"kind": "tri"}, "vocals": {"status": "not_analyzed"}}
        if cache_hit
        else None,
    )
    monkeypatch.setattr(
        rb_anlz_cache, "_store_cached_payload", lambda _s, _m, _p, payload: stored.append(payload)
    )
    monkeypatch.setattr(rb_db, "fetch_cues", lambda _vendor_id: [])
    monkeypatch.setattr(rb_anlz, "_first_tags", lambda _directory: ({"PQTZ": _FakePqtz()}, []))
    monkeypatch.setattr(rb_anlz, "vocals_payload", lambda _path: {"status": "not_analyzed"})

    payload = rb_vendor.build_anlz_payload(content, points=16)

    assert [b["t"] for b in payload["beatgrid"]["beats"]] == [0.47, 0.97]
    if not cache_hit:
        assert [b["t"] for b in stored[0]["beatgrid"]["beats"]] == [0.02, 0.52, 1.02]
