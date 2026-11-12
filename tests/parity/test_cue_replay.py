"""Cue recording replay on an injected monotonic clock (CUES-12240).

[if] a recording replays on a controlled clock [then] events fire in order on time, [else stop].
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apps.parity.cue_recording import (
    CuePressEvent,
    CueRecording,
    CueRecordingError,
    recording_to_json,
    write_recording,
)
from apps.parity.cue_replay import replay_recording, replay_recording_file

pytestmark = pytest.mark.requirement("CUES-12240")

START_S: float = 1000.0
RECORDING: CueRecording = CueRecording(
    track_id="track-7",
    events=(
        CuePressEvent(0.0, "cue", "press"),
        CuePressEvent(250.0, "cue", "release"),
        CuePressEvent(250.0, "hot:A", "press"),
        CuePressEvent(250.0, "hot:B", "press"),
        CuePressEvent(1333.3, "hot:A", "release"),
    ),
)


@dataclass
class FakeClock:
    """Controlled monotonic clock: sleep advances time exactly (synthesized fixture)."""

    now_s: float = START_S
    regress_after_sleeps: int | None = None
    regress_by_s: float = 0.0
    sleeps: int = 0
    dispatched: list[tuple[CuePressEvent, float]] = field(default_factory=list)

    def clock(self) -> float:
        return self.now_s

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now_s += seconds
        if self.sleeps == self.regress_after_sleeps:
            self.now_s -= self.regress_by_s

    def dispatch(self, event: CuePressEvent) -> None:
        self.dispatched.append((event, self.now_s))


def test_events_dispatch_within_one_ms() -> None:
    """[if] replay runs on a controlled clock [then] every event fires under 1 ms of its time, [else stop]."""
    fake = FakeClock()
    assert replay_recording(RECORDING, fake.clock, fake.sleep, fake.dispatch) == len(RECORDING.events)
    assert len(fake.dispatched) == len(RECORDING.events)
    for event, at_s in fake.dispatched:
        assert abs((at_s - START_S) * 1000.0 - event.offset_ms) < 1.0


def test_shared_timestamps_keep_recorded_order() -> None:
    """[if] events share a timestamp [then] replay keeps their recorded order, [else stop]."""
    fake = FakeClock()
    replay_recording(RECORDING, fake.clock, fake.sleep, fake.dispatch)
    assert [event for event, _ in fake.dispatched] == list(RECORDING.events)


def test_regressing_clock_never_fires_early() -> None:
    """[if] the monotonic clock steps backwards [then] no event fires before its time, [else stop]."""
    fake = FakeClock(regress_after_sleeps=1, regress_by_s=0.2)
    replay_recording(RECORDING, fake.clock, fake.sleep, fake.dispatch)
    assert [event for event, _ in fake.dispatched] == list(RECORDING.events)
    for event, at_s in fake.dispatched:
        assert (at_s - START_S) * 1000.0 >= event.offset_ms - 1e-9


def test_invalid_recording_never_dispatches(tmp_path: Path) -> None:
    """[if] recording validation fails [then] replay raises and dispatches nothing, [else stop]."""
    path = tmp_path / "recording.json"
    write_recording(path, RECORDING)
    data = recording_to_json(RECORDING)
    data["events"][-1]["cue_id"] = "hot:Q"
    path.write_text(json.dumps(data), encoding="utf-8")
    fake = FakeClock()
    with pytest.raises(CueRecordingError):
        replay_recording_file(path, fake.clock, fake.sleep, fake.dispatch)
    assert fake.dispatched == []
