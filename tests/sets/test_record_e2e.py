"""End-to-end recorder tests (mocked capture + sources)."""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.sets import record as record_mod
from apps.sets.manifest import read_manifest
from apps.sets.state import Event, SetsState
from tests.waits import THREAD_HANG_GUARD_S


class _FakeSource:
    """Source that emits a canned event per poll."""

    def __init__(self, recorder: record_mod.Recorder, uuids: list[str]):
        self.recorder = recorder
        self._uuids = list(uuids)

    def poll_once(self) -> None:
        if not self._uuids:
            return
        uuid = self._uuids.pop(0)
        self.recorder._emit(
            Event(
                session_id=self.recorder.session_id,
                timestamp_s=self.recorder._rel_ts(),
                wall_clock=self.recorder._wall(),
                deck="A",
                track_stable_id=uuid,
                action="track_loaded",
                source="djay_monitor",
                value={"uuid": uuid},
            )
        )


class _HeartbeatSignallingState(SetsState):
    """A real SetsState that signals the moment a heartbeat row is written.

    The write itself is the real one; the Event only lets the test wait on
    the heartbeat thread's own progress instead of a wall-clock window.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path=db_path)
        self.heartbeat_written = threading.Event()

    def record_event(self, event: Event) -> int:
        row_id = super().record_event(event)
        if event.action == "heartbeat":
            self.heartbeat_written.set()
        return row_id


@pytest.fixture
def test_config() -> record_mod.RecorderConfig:
    return record_mod.RecorderConfig(
        sources=("fake_source",),
        capture_disabled=True,  # no ffmpeg in tests
    )


@pytest.mark.requirement("SET-01")
def test_resolve_session_id_appends_counter_on_collision(tmp_path: Path):
    now = datetime(2026, 4, 17, 21, 30, 0, tzinfo=UTC)
    first = record_mod.resolve_session_id(root=tmp_path, now=now)
    (tmp_path / first).mkdir()
    second = record_mod.resolve_session_id(root=tmp_path, now=now)
    assert second == f"{first}_1"


@pytest.mark.requirement("SET-01")
def test_resolve_session_id_rejects_malformed(tmp_path: Path):
    with pytest.raises(ValueError):
        record_mod.resolve_session_id("bad-id", root=tmp_path)


@pytest.mark.requirement("SET-01")
def test_start_writes_session_start_event_and_pid(
    tmp_path: Path,
    test_config: record_mod.RecorderConfig,
):
    state = SetsState(db_path=tmp_path / "sets.db")
    recorder = record_mod.start(
        config=test_config,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, ["u-1", "u-2"]),
        },
    )
    # pid file is present while live
    assert (recorder.session_dir / "recorder.pid").exists()
    events = state.fetch_events(recorder.session_id)
    assert events[0].action == "session_start"
    assert events[0].source == "recorder"
    assert events[0].value["sources"] == ["fake_source"]
    # Share state is private by default
    row = state.get_session(recorder.session_id)
    assert row is not None and row.share_state == "private"


@pytest.mark.requirement("SET-01")
def test_run_poll_iteration_emits_source_events(
    tmp_path: Path,
    test_config: record_mod.RecorderConfig,
):
    state = SetsState(db_path=tmp_path / "sets.db")
    recorder = record_mod.start(
        config=test_config,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, ["u-1", "u-2"]),
        },
    )
    recorder.run_poll_iteration()
    recorder.run_poll_iteration()
    loaded = state.fetch_events(recorder.session_id, action="track_loaded")
    assert [e.track_stable_id for e in loaded] == ["u-1", "u-2"]
    # JSONL mirrors the DB.
    lines = (recorder.session_dir / "timeline.jsonl").read_text().splitlines()
    assert len(lines) >= 3  # session_start + 2 track_loaded
    first_obj = json.loads(lines[0])
    assert first_obj["action"] == "session_start"


@pytest.mark.requirement("SET-01")
def test_stop_writes_manifest_and_ends_session(
    tmp_path: Path,
    test_config: record_mod.RecorderConfig,
):
    state = SetsState(db_path=tmp_path / "sets.db")
    recorder = record_mod.start(
        config=test_config,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, ["u-1"]),
        },
    )
    recorder.run_poll_iteration()
    manifest = record_mod.stop(recorder)
    assert manifest.session_id == recorder.session_id
    assert manifest.ended_at is not None
    assert manifest.share_state == "private"
    assert manifest.event_count >= 2  # session_start + track_loaded + session_end
    assert not (recorder.session_dir / "recorder.pid").exists()
    # manifest.json written on disk
    disk = read_manifest(recorder.session_dir)
    assert disk.session_id == recorder.session_id


@pytest.mark.requirement("SET-01")
def test_resume_continues_same_session(
    tmp_path: Path,
    test_config: record_mod.RecorderConfig,
):
    state = SetsState(db_path=tmp_path / "sets.db")
    recorder = record_mod.start(
        config=test_config,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, ["u-1"]),
        },
    )
    recorder.run_poll_iteration()
    sid = recorder.session_id

    # Simulate a crash: we don't call stop(); just drop the recorder.
    resumed = record_mod.resume(
        sid,
        config=test_config,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, ["u-2"]),
        },
    )
    assert resumed.session_id == sid
    resumed.run_poll_iteration()
    # We see the session_resume marker + both track_loaded events.
    events = state.fetch_events(sid)
    actions = [e.action for e in events]
    assert "session_start" in actions
    assert "session_resume" in actions
    loaded = [e for e in events if e.action == "track_loaded"]
    assert [e.track_stable_id for e in loaded] == ["u-1", "u-2"]


@pytest.mark.requirement("SET-01")
def test_timeline_jsonl_tolerates_malformed_trailing_line(
    tmp_path: Path,
):
    from apps.sets.record import TimelineJsonl

    path = tmp_path / "timeline.jsonl"
    path.write_text('{"action":"ok"}\n{broken\n')
    tl = TimelineJsonl(path)
    events = list(tl.iter_events())
    assert events == [{"action": "ok"}]


@pytest.mark.requirement("SET-01")
def test_status_reports_active_session_when_pid_present(
    tmp_path: Path,
    test_config: record_mod.RecorderConfig,
):
    state = SetsState(db_path=tmp_path / "sets.db")
    recorder = record_mod.start(
        config=test_config,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, []),
        },
    )
    info = record_mod.status(sets_root=tmp_path / "sets")
    assert info["active"] is True
    assert info["session_id"] == recorder.session_id
    record_mod.stop(recorder)
    info_after = record_mod.status(sets_root=tmp_path / "sets")
    assert info_after == {"active": False}


@pytest.mark.requirement("SET-01")
def test_recorder_heartbeat_thread_emits_heartbeat(
    tmp_path: Path,
):
    """The heartbeat thread issues at least one heartbeat event."""
    cfg = record_mod.RecorderConfig(
        sources=("fake_source",),
        capture_disabled=True,
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
    )
    state = _HeartbeatSignallingState(db_path=tmp_path / "sets.db")
    recorder = record_mod.start(
        config=cfg,
        sets_root=tmp_path / "sets",
        state=state,
        source_factories={
            "fake_source": lambda rec: _FakeSource(rec, []),
        },
    )
    recorder.start_threads()
    try:
        # Wait on the heartbeat write itself: a slow runner only delays it.
        assert state.heartbeat_written.wait(THREAD_HANG_GUARD_S), (
            f"HANG: no heartbeat row written within {THREAD_HANG_GUARD_S}s of "
            f"start_threads() (heartbeat thread alive="
            f"{recorder._heartbeat_thread is not None and recorder._heartbeat_thread.is_alive()})"
        )
    finally:
        record_mod.stop(recorder)
    hb = state.fetch_events(recorder.session_id, action="heartbeat")
    assert len(hb) >= 1, f"a heartbeat was signalled but the timeline holds none: {hb}"
