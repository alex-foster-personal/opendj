"""LIGHT-30 MIDI beat clock acceptance tests (issue #9882).

[if] the clock spacing, Start, Song Position Pointer plus Continue, or Stop truncation differs from the MIDI 1.0 definitions [then] fail, [else stop].
"""

from __future__ import annotations

import itertools

import pytest

from apps.lighting.beat_clock import BeatEvent
from apps.lighting.midi_clock import (
    CONTINUE,
    PULSES_PER_BEAT,
    SONG_POSITION_POINTER,
    START,
    STOP,
    TIMING_CLOCK,
    ClockMessage,
    clocks_for_beats,
    schedule_followed_deck,
    song_position,
    transport_start,
    transport_stop,
)

pytestmark = [pytest.mark.requirement("LIGHT-10460")]

HOST_NS = 7_000_000_000
NS_PER_MS = 1_000_000


def _beat(index: int, bpm: float = 125.0, at_host_ns: int = HOST_NS, deck: str = "A") -> BeatEvent:
    bar_beat = index % 4 + 1
    return BeatEvent(
        deck=deck,
        beat_index=index,
        bar_beat=bar_beat,
        is_downbeat=bar_beat == 1,
        bpm=bpm,
        at_host_ns=at_host_ns,
    )


def _beats(count: int, bpm: float = 125.0, first_index: int = 0) -> list[BeatEvent]:
    beat_ns = 60_000_000_000 / bpm
    return [_beat(first_index + k, bpm, HOST_NS + round(k * beat_ns)) for k in range(count)]


def _is_clock(message: ClockMessage) -> bool:
    return message.data == bytes((TIMING_CLOCK,))


def test_one_beat_at_125_bpm_gives_24_clocks_20ms_apart() -> None:
    """[if] one beat at 125 BPM is scheduled [then] 24 clocks are 20.0 ms apart within 1 microsecond, [else stop]."""
    clocks = clocks_for_beats([_beat(0, 125.0)])
    assert len(clocks) == PULSES_PER_BEAT == 24
    assert all(_is_clock(message) for message in clocks)
    assert clocks[0].at_host_ns == HOST_NS
    for earlier, later in itertools.pairwise(clocks):
        assert abs((later.at_host_ns - earlier.at_host_ns) - 20 * NS_PER_MS) <= 1_000


def test_starting_on_a_downbeat_sends_start_first() -> None:
    """[if] the deck starts at beat 0 of a bar [then] the first byte is 0xFA, [else stop]."""
    schedule = schedule_followed_deck(_beats(2), starting=True)
    assert schedule[0].data[0] == START == 0xFA
    assert schedule[0].at_host_ns == HOST_NS
    assert all(_is_clock(message) for message in schedule[1:])


def test_resuming_at_beat_37_sends_song_position_then_continue() -> None:
    """[if] the deck resumes at beat 37 [then] an SPP of 148 sixteenths (bytes 0x14 0x01) precedes 0xFB, [else stop]."""
    messages = transport_start(_beat(37))
    assert [message.data for message in messages] == [
        bytes((SONG_POSITION_POINTER, 0x14, 0x01)),
        bytes((CONTINUE,)),
    ]
    assert song_position(37) == bytes((0xF2, 0x14, 0x01))
    schedule = schedule_followed_deck(_beats(1, first_index=37), starting=True)
    assert schedule[0].data[0] == SONG_POSITION_POINTER
    assert schedule[1].data == bytes((CONTINUE,))
    assert schedule[2].data == bytes((TIMING_CLOCK,))


def test_stop_truncates_the_schedule_and_no_clock_follows() -> None:
    """[if] the deck stops [then] 0xFC is scheduled and no further clocks follow, [else stop]."""
    stop_ns = HOST_NS + 500 * NS_PER_MS
    schedule = schedule_followed_deck(_beats(4), starting=False, stop_at_host_ns=stop_ns)
    assert schedule[-1] == ClockMessage(stop_ns, bytes((STOP,)))
    assert STOP == 0xFC
    clocks = [message for message in schedule if _is_clock(message)]
    assert clocks, "clocks before the stop are kept"
    assert all(message.at_host_ns < stop_ns for message in clocks)
    assert schedule == sorted(schedule, key=lambda message: message.at_host_ns)
    assert transport_stop([], stop_ns) == [ClockMessage(stop_ns, bytes((STOP,)))]


def test_tempo_change_takes_effect_from_the_next_beats_clocks() -> None:
    """[if] the second beat carries a new BPM [then] its clocks use the new spacing and the first beat's keep the old, [else stop]."""
    first = _beat(0, 120.0, HOST_NS)
    second = _beat(1, 150.0, HOST_NS + 500 * NS_PER_MS)
    clocks = clocks_for_beats([first, second])
    old_gaps = {later.at_host_ns - earlier.at_host_ns for earlier, later in itertools.pairwise(clocks[:24])}
    new_gaps = {later.at_host_ns - earlier.at_host_ns for earlier, later in itertools.pairwise(clocks[24:])}
    assert all(abs(gap - 500 * NS_PER_MS / 24) <= 1 for gap in old_gaps)
    assert all(abs(gap - 400 * NS_PER_MS / 24) <= 1 for gap in new_gaps)


def test_song_position_past_the_pointer_limit_and_mixed_decks_are_refused() -> None:
    """[if] a beat is past 16383 sixteenths or beats come from two decks [then] ValueError names the limit or the decks, [else stop]."""
    assert song_position(4095) == bytes((0xF2, 0x7C, 0x7F))
    with pytest.raises(ValueError, match="16383"):
        song_position(4096)
    with pytest.raises(ValueError, match="one deck"):
        clocks_for_beats([_beat(0, deck="A"), _beat(1, deck="B")])


@pytest.mark.requirement("TESTINFRA-14162")
@pytest.mark.parametrize(
    ("bpm", "field"),
    [
        (float("nan"), "bpm"),
        (float("inf"), "bpm"),
        (float("-inf"), "bpm"),
    ],
)
def test_non_finite_bpm_refuses_scheduling(bpm: float, field: str) -> None:
    """[if] a beat has non-finite BPM [then] clock scheduling refuses naming bpm, [else stop]."""
    with pytest.raises(ValueError, match=field):
        clocks_for_beats([_beat(0, bpm=bpm)])


@pytest.mark.requirement("TESTINFRA-14162")
def test_regressing_beat_host_times_refuse_scheduling() -> None:
    """[if] followed-deck beats regress in host time [then] scheduling refuses with at_host_ns, [else stop]."""
    beat_ns = 60_000_000_000 / 125.0
    later = _beat(0, 125.0, HOST_NS + round(beat_ns))
    earlier = _beat(1, 125.0, HOST_NS)
    with pytest.raises(ValueError, match="at_host_ns"):
        clocks_for_beats([later, earlier])


@pytest.mark.requirement("TESTINFRA-14162")
def test_stop_before_the_first_clock_keeps_stop_and_no_clocks() -> None:
    """[if] stop is before the first generated clock [then] only Stop is returned with no clocks, [else stop]."""
    stop_ns = HOST_NS - 1
    schedule = schedule_followed_deck(_beats(2), starting=True, stop_at_host_ns=stop_ns)
    assert schedule == [ClockMessage(stop_ns, bytes((STOP,)))]
    assert not any(_is_clock(message) for message in schedule)
