"""SETREC-04: transport and tempo controls land in controls.jsonl."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.sets import record as record_mod
from apps.sets.control_events import ControlEventV2, coalesce_continuous
from apps.sets.engine_control import stamp_from_cmd
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState


@pytest.mark.requirement("SETREC-11805b")
def test_scripted_play_seek_tempo_logs_one_event_per_trigger(tmp_path: Path):
    """[if] play seek tempo fire once each [then] controls.jsonl has one row each, [else stop]."""
    service = RecorderService(sets_root=tmp_path, db_path=tmp_path / "sets.db", capture_enabled=False)
    sid = "2026-04-17T21-30-00"
    service.start(session_id=sid, source="none", sources=())
    sr = 48_000
    stamps = [
        stamp_from_cmd("play", deck=1, sample=0, sample_rate=sr, playing=True),
        stamp_from_cmd("seek", deck=1, sample=sr, sample_rate=sr, position_ms=1500.0),
        stamp_from_cmd("tempo", deck=1, sample=2 * sr, sample_rate=sr, ratio=1.06),
        stamp_from_cmd("cue", deck=1, sample=3 * sr, sample_rate=sr),
    ]
    assert all(s is not None for s in stamps)
    service.drain_control_stamps(sid, [s for s in stamps if s is not None])
    lines = service._recorder.controls.read_lines()  # type: ignore[union-attr]
    controls = {row["control"]: row for row in lines}
    assert controls["deck.1.play"]["value"] is True
    assert controls["deck.1.seek_ms"]["sample"] == sr
    assert controls["deck.1.tempo"]["value"] == 1.06
    assert controls["deck.1.cue"]["kind"] == "trigger"
    assert [row["seq"] for row in lines] == [0, 1, 2, 3]


@pytest.mark.requirement("SETREC-11805b")
def test_tempo_fader_burst_coalesces_inside_10ms_quantum(tmp_path: Path):
    """[if] tempo fader moves inside 10ms [then] first and last samples kept, [else stop]."""
    sr = 48_000
    quantum = int(sr * 0.01)
    stamps = [
        stamp_from_cmd("tempo_fader", deck=1, sample=0, sample_rate=sr, value=0.4),
        stamp_from_cmd("tempo_fader", deck=1, sample=quantum // 2, sample_rate=sr, value=0.5),
        stamp_from_cmd("tempo_fader", deck=1, sample=quantum - 1, sample_rate=sr, value=0.9),
    ]
    merged = coalesce_continuous([s for s in stamps if s], sample_rate=sr)
    assert len(merged) == 2
    assert merged[0].value == 0.4
    assert merged[1].value == 0.9


@pytest.mark.requirement("SETREC-11805b")
def test_drain_rejects_seq_gap(tmp_path: Path):
    """[if] controls.jsonl would get a seq gap [then] drain raises, [else stop]."""
    state = SetsState(db_path=tmp_path / "sets.db")
    started = datetime(2026, 4, 17, 21, 30, 0, tzinfo=UTC)
    recorder = record_mod._build_recorder(
        session_id="2026-04-17T21-30-00",
        sets_root=tmp_path,
        state=state,
        config=record_mod.RecorderConfig(capture_disabled=True),
        started_at=started,
    )
    state.open_session(recorder.session_id, capture_device="none")
    sr = 48_000
    first = stamp_from_cmd("play", deck=1, sample=0, sample_rate=sr, playing=True)
    assert first is not None
    recorder.drain_control_stamps([first])
    gap_stamp = stamp_from_cmd("play", deck=1, sample=sr, sample_rate=sr, playing=False)
    assert gap_stamp is not None
    recorder._control_seq = 2  # simulate gap
    with pytest.raises(Exception, match="expected seq"):
        recorder.drain_control_stamps([gap_stamp])


@pytest.mark.requirement("SETREC-11805b")
def test_controls_mirrored_to_sets_db(tmp_path: Path):
    """[if] drain runs [then] control_events table matches jsonl, [else stop]."""
    service = RecorderService(sets_root=tmp_path, db_path=tmp_path / "sets.db", capture_enabled=False)
    sid = "2026-04-17T21-30-01"
    service.start(session_id=sid, source="none", sources=())
    stamp = stamp_from_cmd("master_tempo", deck=1, sample=0, sample_rate=48_000, enabled=True)
    assert stamp is not None
    service.drain_control_stamps(sid, [stamp])
    state = SetsState(db_path=tmp_path / "sets.db")
    rows = state.fetch_control_events(sid)
    assert len(rows) == 1
    assert rows[0]["control"] == "deck.1.master_tempo"
    assert rows[0]["seq"] == 0
    assert rows[0]["sample"] == 0
    ev = ControlEventV2(
        session_id=sid,
        seq=rows[0]["seq"],
        sample=rows[0]["sample"],
        sample_rate=rows[0]["sample_rate"],
        wall_clock=rows[0]["wall_clock"],
        control=rows[0]["control"],
        value=rows[0]["value"],
        kind=rows[0]["kind"],
        source=rows[0]["source"],
        deck=rows[0]["deck"],
    )
    ev.validate()
