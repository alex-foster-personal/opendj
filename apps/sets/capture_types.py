"""Types shared by the set-audio capture backends (:mod:`apps.sets.capture`)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CaptureHandle:
    """Live handle returned by :func:`start_capture`.

    Stores the ``Popen`` plus the capture argv (so tests can assert on
    it), the resolved stderr log path, the open log file handle so it
    can be closed in :func:`stop_capture`, and which backend runs it.
    """

    proc: subprocess.Popen
    argv: list[str]
    stderr_log: Path
    log_fh: IO[bytes]
    backend: Literal["odj-audio", "ffmpeg"] = "ffmpeg"


def is_loopback_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in LOOPBACK_NAME_HINTS)


__all__ = [
    "LOOPBACK_NAME_HINTS",
    "CaptureBackend",
    "CaptureHandle",
    "CaptureUnavailable",
    "InputDevice",
    "is_loopback_name",
]
