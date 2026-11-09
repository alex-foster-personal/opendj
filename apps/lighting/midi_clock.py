"""MIDI beat clock scheduler following the followed deck (LIGHT-30 in the lighting spec).

Pure: turns the beat clock's ``BeatEvent`` list for one deck into ``(at_host_ns, bytes)``
messages. Opening a MIDI port is host-bound (L32) and lives elsewhere.

Message bytes come from the MIDI 1.0 System Real Time and System Common definitions:
Timing Clock 0xF8 at 24 pulses per quarter note, Start 0xFA, Continue 0xFB, Stop 0xFC, and
Song Position Pointer 0xF2 followed by two 7-bit data bytes (LSB first) counting sixteenth
notes. The deck's beat is the quarter note. Tempo follows each beat's own BPM, so a tempo
change takes effect from the next clock after the beat that carries it.

Following one deck (not the fader mix) is a design choice: a mix-weighted tempo is not a
well-defined clock, so the followed deck wins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from apps.lighting.beat_clock import BeatEvent

TIMING_CLOCK = 0xF8
START = 0xFA
CONTINUE = 0xFB
STOP = 0xFC
SONG_POSITION_POINTER = 0xF2

PULSES_PER_BEAT = 24
SIXTEENTHS_PER_BEAT = 4
SONG_POSITION_MAX = 0x3FFF
NS_PER_MINUTE = 60_000_000_000


@dataclass(frozen=True, order=True)
class ClockMessage:
    """One MIDI message and the host time it is due."""

    at_host_ns: int
    data: bytes


# ----------------------------------------------------------------------------- helpers


def _clock_interval_ns(bpm: float) -> float:
    if bpm <= 0:
        raise ValueError(f"bpm must be positive to space clocks, got {bpm}")
    return NS_PER_MINUTE / (bpm * PULSES_PER_BEAT)


def _clocks_for_beat(beat: BeatEvent) -> list[ClockMessage]:
    interval_ns = _clock_interval_ns(beat.bpm)
    return [
        ClockMessage(at_host_ns=beat.at_host_ns + round(pulse * interval_ns), data=bytes((TIMING_CLOCK,)))
        for pulse in range(PULSES_PER_BEAT)
    ]


# ----------------------------------------------------------------------------- public api


def song_position(beat_index: int) -> bytes:
    """Song Position Pointer for a beat: 0xF2, then the sixteenth count as LSB and MSB (7 bits each)."""
    if beat_index < 0:
        raise ValueError(f"beat_index must be 0 or more, got {beat_index}")
    sixteenths = beat_index * SIXTEENTHS_PER_BEAT
    if sixteenths > SONG_POSITION_MAX:
        raise ValueError(
            f"beat {beat_index} is {sixteenths} sixteenths, past the Song Position Pointer limit of {SONG_POSITION_MAX}"
        )
    return bytes((SONG_POSITION_POINTER, sixteenths & 0x7F, sixteenths >> 7))


def transport_start(first_beat: BeatEvent) -> list[ClockMessage]:
    """Messages that precede the first clock when the deck starts playing at ``first_beat``.

    From a downbeat: Start (the receiver resets to the top). From anywhere else: a Song Position
    Pointer for the beat, then Continue, both due at the beat's host time and before its clocks.
    """
    if first_beat.is_downbeat:
        return [ClockMessage(first_beat.at_host_ns, bytes((START,)))]
    return [
        ClockMessage(first_beat.at_host_ns, song_position(first_beat.beat_index)),
        ClockMessage(first_beat.at_host_ns, bytes((CONTINUE,))),
    ]


def clocks_for_beats(beats: Sequence[BeatEvent]) -> list[ClockMessage]:
    """24 Timing Clocks per beat, spaced by that beat's BPM, in host-time order."""
    if len({beat.deck for beat in beats}) > 1:
        raise ValueError("clocks follow one deck; got beats from " + ", ".join(sorted({b.deck for b in beats})))
    messages: list[ClockMessage] = []
    for beat in beats:
        messages.extend(_clocks_for_beat(beat))
    return messages


def transport_stop(scheduled: Sequence[ClockMessage], at_host_ns: int) -> list[ClockMessage]:
    """Drop every message due at or after ``at_host_ns`` and end the schedule with Stop."""
    kept = [message for message in scheduled if message.at_host_ns < at_host_ns]
    kept.append(ClockMessage(at_host_ns, bytes((STOP,))))
    return kept


def schedule_followed_deck(
    beats: Sequence[BeatEvent],
    *,
    starting: bool,
    stop_at_host_ns: int | None = None,
) -> list[ClockMessage]:
    """Full schedule for a window of the followed deck's beats.

    ``starting`` is true when the deck began playing with the first beat of this window, which
    prepends Start or Song Position Pointer plus Continue. ``stop_at_host_ns`` truncates the
    schedule and appends Stop.
    """
    messages: list[ClockMessage] = []
    if beats and starting:
        messages.extend(transport_start(beats[0]))
    messages.extend(clocks_for_beats(beats))
    if stop_at_host_ns is not None:
        messages = transport_stop(messages, stop_at_host_ns)
    return messages
