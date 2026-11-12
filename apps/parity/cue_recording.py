"""Cue-press recordings: timestamped press and release events in a versioned JSON file.

A recording keeps the performance intent captured on one track (which cue was pressed or
released, and when) as a portable artifact instead of browser-only state. Offsets are
milliseconds from the start of the recording and never decrease. Loading refuses any
schema version other than the current one.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CUE_RECORDING_SCHEMA_VERSION: int = 1
CUE_ACTIONS: frozenset[str] = frozenset({"press", "release"})
CUE_ID_PATTERN: re.Pattern[str] = re.compile(r"^(cue|hot:[A-H])$")


class CueRecordingError(ValueError):
    """A cue recording or one of its events is invalid."""


@dataclass(frozen=True)
class CuePressEvent:
    """One press or release of the main cue (``cue``) or a hot cue (``hot:A`` .. ``hot:H``)."""

    offset_ms: float
    cue_id: str
    action: str

    def __post_init__(self) -> None:
        if isinstance(self.offset_ms, bool) or not isinstance(self.offset_ms, int | float):
            raise CueRecordingError(f"offset_ms must be a number, got {self.offset_ms!r}")
        if not math.isfinite(self.offset_ms) or self.offset_ms < 0:
            raise CueRecordingError(f"offset_ms must be finite and not negative, got {self.offset_ms!r}")
        if not isinstance(self.cue_id, str) or not CUE_ID_PATTERN.match(self.cue_id):
            raise CueRecordingError(f"invalid cue identifier {self.cue_id!r}")
        if self.action not in CUE_ACTIONS:
            raise CueRecordingError(f"action must be press or release, got {self.action!r}")


@dataclass(frozen=True)
class CueRecording:
    """Ordered cue-press events recorded against one track."""

    track_id: str
    events: tuple[CuePressEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.track_id, str) or not self.track_id.strip():
            raise CueRecordingError("track_id must be a non-empty string")
        for earlier, later in zip(self.events, self.events[1:], strict=False):
            if later.offset_ms < earlier.offset_ms:
                raise CueRecordingError(f"event offsets must not decrease: {earlier.offset_ms} then {later.offset_ms}")


# -----------------------------------------------------------------------------
# JSON


def recording_to_json(recording: CueRecording) -> dict[str, Any]:
    """Return the versioned JSON object for ``recording``."""
    return {
        "schema_version": CUE_RECORDING_SCHEMA_VERSION,
        "track_id": recording.track_id,
        "events": [
            {"offset_ms": event.offset_ms, "cue_id": event.cue_id, "action": event.action} for event in recording.events
        ],
    }


def recording_from_json(data: object) -> CueRecording:
    """Return the recording in ``data``; raise ``CueRecordingError`` on any invalid field."""
    if not isinstance(data, dict):
        raise CueRecordingError("a cue recording must be a JSON object")
    version = data.get("schema_version")
    if version != CUE_RECORDING_SCHEMA_VERSION:
        raise CueRecordingError(
            f"unsupported cue recording schema_version {version!r}; expected {CUE_RECORDING_SCHEMA_VERSION}"
        )
    events = data.get("events")
    if not isinstance(events, list):
        raise CueRecordingError("events must be a JSON list")
    parsed: list[CuePressEvent] = []
    for event in events:
        if not isinstance(event, dict) or set(event) != {"offset_ms", "cue_id", "action"}:
            raise CueRecordingError(f"event must have exactly offset_ms, cue_id and action: {event!r}")
        parsed.append(CuePressEvent(event["offset_ms"], event["cue_id"], event["action"]))
    track_id = data.get("track_id")
    if not isinstance(track_id, str):
        raise CueRecordingError("track_id must be a string")
    return CueRecording(track_id=track_id, events=tuple(parsed))


def write_recording(path: Path, recording: CueRecording) -> None:
    """Write ``recording`` to ``path`` as versioned JSON."""
    path.write_text(json.dumps(recording_to_json(recording), indent=2) + "\n", encoding="utf-8")


def read_recording(path: Path) -> CueRecording:
    """Return the validated recording stored at ``path``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CueRecordingError(f"{path}: not valid JSON") from error
    return recording_from_json(data)
