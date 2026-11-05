"""Link host time and the session timeline math (LINK-12).

Ableton Link (and so Carabiner) stamps every time in MICROSECONDS of the
platform's raw monotonic clock: on macOS that is ``mach_absolute_time`` scaled
by ``mach_timebase_info``. Python's ``time.monotonic`` is not guaranteed to be
that clock (it is rebased and its source changed across releases), so this
module reads ``mach_absolute_time`` itself through ctypes. On Linux (B2BPAIR-09)
Link's host clock is ``CLOCK_MONOTONIC_RAW``: measured Mon 12 Oct 2026 against the
Carabiner 1.2.0 Linux build on nucbox, ``:start + :beat * 60e6 / :bpm`` matched
``CLOCK_MONOTONIC_RAW`` to within 0.3 ms while ``CLOCK_MONOTONIC`` was 19.7 s off,
and Carabiner's own ``beat-at-time`` agreed. On any other platform
:func:`link_now_us` raises, because a guessed clock would put every beat in the
wrong place without a word.

Carabiner's ``status`` line carries ``:start`` (the Link time of beat 0) and
``:bpm``; with those two the beat at any Link time is a line, so the routes can
answer beat and phase without a round trip per question. Carabiner's own
``beat-at-time`` example agrees: ``(73746356220 - 73743731220) us`` at 120 BPM
is 5.25 beats.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from functools import cache

#: Link's session quantum for a DJ bar (4 beats). Every role uses it.
DEFAULT_QUANTUM: float = 4.0


class _MachTimebaseInfo(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def link_host_clock_available() -> bool:
    """True where ``link_now_us`` can read Link host time (macOS mach clock, Linux MONOTONIC_RAW)."""
    return sys.platform == "darwin" or sys.platform.startswith("linux")


@cache
def _mach_clock() -> tuple[ctypes._CFuncPtr, float]:
    if sys.platform != "darwin":
        raise RuntimeError(
            f"Link host time needs macOS mach_absolute_time or Linux CLOCK_MONOTONIC_RAW; this is {sys.platform}. "
            "Carabiner on another platform uses a different clock, which this build does not read."
        )
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    absolute = libc.mach_absolute_time
    absolute.restype = ctypes.c_uint64
    absolute.argtypes = []
    info = _MachTimebaseInfo()
    if libc.mach_timebase_info(ctypes.byref(info)) != 0 or info.denom == 0:
        raise RuntimeError("mach_timebase_info failed; Link host time cannot be read")
    # ticks * numer / denom = nanoseconds; / 1000 = microseconds.
    return absolute, info.numer / info.denom / 1000.0


def link_now_us() -> int:
    """Link host time now, integer microseconds (the unit Carabiner speaks)."""
    if not link_host_clock_available():
        raise RuntimeError(
            f"Link host time needs macOS mach_absolute_time or Linux CLOCK_MONOTONIC_RAW; this is {sys.platform}."
        )
    if sys.platform.startswith("linux"):
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    absolute, ticks_to_us = _mach_clock()
    return int(absolute() * ticks_to_us)


def beat_at_time(start_us: float, bpm: float, at_us: float) -> float:
    """Session beat at Link time ``at_us`` on a timeline with beat 0 at ``start_us``."""
    if not (math.isfinite(bpm) and bpm > 0):
        raise ValueError(f"beat_at_time needs a positive bpm, got {bpm}")
    return (at_us - start_us) / 60_000_000.0 * bpm


def phase_of(beat: float, quantum: float = DEFAULT_QUANTUM) -> float:
    """Beat position inside its bar, in [0, quantum); negative beats wrap too."""
    if not quantum > 0:
        raise ValueError(f"quantum must be > 0, got {quantum}")
    phase = math.fmod(beat, quantum)
    return phase + quantum if phase < 0 else phase
