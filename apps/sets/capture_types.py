"""Types shared by the set-audio capture backends (:mod:`apps.sets.capture`)."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

# Name fragments of virtual loopback inputs. A loopback carries the master
# output back in as an input, so it is the right default for a set recording;
# a microphone would record the room. Matched case-insensitively.
LOOPBACK_NAME_HINTS: tuple[str, ...] = ("blackhole", "loopback", "soundflower")


class CaptureUnavailable(RuntimeError):
    """Audio capture cannot be measured or started here. Never an empty result."""


@dataclass(frozen=True)
class CaptureBackend:
    """The program that records: ``kind`` and the executable to run."""

    kind: Literal["odj-audio", "ffmpeg"]
    exe: str


@dataclass(frozen=True)
class InputDevice:
    """One audio input, as the REC picker shows it, in its backend's order."""

    index: int
    name: str
    loopback: bool


#: Where a capture is, as REC shows it. ``waiting_permission``: macOS's
#: first-run microphone prompt is up and nothing is being written (SET-11).
CaptureStateName = Literal["starting", "waiting_permission", "recording", "stopped", "failed"]


class CaptureState:
    """The capture's state, set by the thread reading its output."""

    def __init__(self, value: CaptureStateName) -> None:
        self._lock = threading.Lock()
        self._value: CaptureStateName = value
        self._error: str | None = None
        self.reader: threading.Thread | None = None

    @property
    def value(self) -> CaptureStateName:
        with self._lock:
            return self._value

    @property
    def error(self) -> str | None:
        """Why the capture failed, in the engine's words, when it said."""
        with self._lock:
            return self._error

    def set(self, value: CaptureStateName) -> None:
        with self._lock:
            self._value = value

    def fail(self, error: str) -> None:
        with self._lock:
            self._value = "failed"
            self._error = error


@dataclass(frozen=True)
class CaptureHandle:
    """Live handle returned by :func:`start_capture`.

    Stores the ``Popen`` plus the capture argv (so tests can assert on
    it), the resolved stderr log path, the open log file handle so it
    can be closed in :func:`stop_capture`, which backend runs it, and its
    state (ffmpeg reports none, so it counts as recording once running).
    """

    proc: subprocess.Popen
    argv: list[str]
    stderr_log: Path
    log_fh: IO[bytes]
    backend: Literal["odj-audio", "ffmpeg"] = "ffmpeg"
    state: CaptureState = field(default_factory=lambda: CaptureState("recording"))

    def current_state(self) -> CaptureStateName:
        """The state, or ``failed`` once the process is gone without stopping."""
        value = self.state.value
        if value != "stopped" and self.proc.poll() is not None:
            return "failed"
        return value


def is_loopback_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in LOOPBACK_NAME_HINTS)


__all__ = [
    "LOOPBACK_NAME_HINTS",
    "CaptureBackend",
    "CaptureHandle",
    "CaptureState",
    "CaptureStateName",
    "CaptureUnavailable",
    "InputDevice",
    "is_loopback_name",
]
