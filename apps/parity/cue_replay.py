"""Replay a cue-press recording on an injected monotonic clock.

Pure scheduler: the caller injects ``clock`` (monotonic seconds), ``sleep`` and the cue
``dispatch`` callback, so no hardware or wall clock is involved. Each event is due at
``start + offset_ms`` on the clock read when replay starts, and is dispatched only once
the clock reads at or past that time, so a clock that steps backwards delays events
instead of firing them early. Events sharing an offset keep their recorded order.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from apps.parity.cue_recording import CuePressEvent, CueRecording, read_recording

MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]
CueDispatcher = Callable[[CuePressEvent], None]


def replay_recording(recording: CueRecording, clock: MonotonicClock, sleep: Sleeper, dispatch: CueDispatcher) -> int:
    """Dispatch every event of ``recording`` at its offset; return how many were dispatched."""
    start_s = clock()
    for event in recording.events:
        due_s = start_s + event.offset_ms / 1000.0
        now_s = clock()
        while now_s < due_s:
            sleep(due_s - now_s)
            now_s = clock()
        dispatch(event)
    return len(recording.events)


def replay_recording_file(path: Path, clock: MonotonicClock, sleep: Sleeper, dispatch: CueDispatcher) -> int:
    """Validate the recording at ``path`` in full, then replay it; invalid files dispatch nothing."""
    return replay_recording(read_recording(path), clock, sleep, dispatch)
