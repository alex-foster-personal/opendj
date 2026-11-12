"""Cue-press recording model and versioned JSON file (CUES-12239).

[if] a cue-press recording is written and read [then] it round-trips with version and track, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.parity.cue_recording import (
    CUE_RECORDING_SCHEMA_VERSION,
    CuePressEvent,
    CueRecording,
    CueRecordingError,
    read_recording,
    recording_from_json,
    recording_to_json,
    write_recording,
)

pytestmark = pytest.mark.requirement("CUES-12239")

RECORDING: CueRecording = CueRecording(
    track_id="track-42",
    events=(
        CuePressEvent(0.0, "cue", "press"),
        CuePressEvent(120.5, "cue", "release"),
        CuePressEvent(120.5, "hot:B", "press"),
        CuePressEvent(980.25, "hot:B", "release"),
    ),
)


def test_press_release_sequence_round_trips(tmp_path: Path) -> None:
    """[if] a press and release sequence round-trips through JSON [then] order and offsets match, [else stop]."""
    path = tmp_path / "recording.json"
    write_recording(path, RECORDING)
    loaded = read_recording(path)
    assert loaded == RECORDING
    assert [e.offset_ms for e in loaded.events] == [0.0, 120.5, 120.5, 980.25]


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_unknown_schema_version_is_refused(version: object) -> None:
    """[if] a JSON recording has an unknown schema version [then] loading raises, [else stop]."""
    data = recording_to_json(RECORDING)
    data["schema_version"] = version
    with pytest.raises(CueRecordingError):
        recording_from_json(data)


@pytest.mark.parametrize(
    ("offset_ms", "cue_id", "action"),
    [
        (-1.0, "cue", "press"),
        (float("nan"), "cue", "press"),
        (5.0, "hot:Z", "press"),
        (5.0, "", "press"),
        (5.0, "cue", "hold"),
    ],
)
def test_invalid_events_are_refused(offset_ms: float, cue_id: str, action: str) -> None:
    """[if] an event has a negative time or invalid cue id [then] the model raises, [else stop]."""
    with pytest.raises(CueRecordingError):
        CuePressEvent(offset_ms, cue_id, action)
    data = recording_to_json(RECORDING)
    data["events"][1] = {"offset_ms": offset_ms, "cue_id": cue_id, "action": action}
    with pytest.raises(CueRecordingError):
        recording_from_json(data)


def test_decreasing_offsets_are_refused() -> None:
    """[if] event offsets decrease [then] the recording raises, [else stop]."""
    with pytest.raises(CueRecordingError):
        CueRecording("t", (CuePressEvent(10.0, "cue", "press"), CuePressEvent(9.0, "cue", "release")))


def test_written_json_carries_version_and_track(tmp_path: Path) -> None:
    """[if] a recording is written [then] its JSON has schema version and track identity, [else stop]."""
    path = tmp_path / "recording.json"
    write_recording(path, RECORDING)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == CUE_RECORDING_SCHEMA_VERSION
    assert data["track_id"] == "track-42"
