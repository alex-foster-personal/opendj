"""LIGHT-31 MIDI timecode and ArtTimeCode acceptance tests (issue #9883).

[if] the Full Frame, quarter-frame order, seek ordering or rate validation differs from MIDI 1.0 MTC [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.lighting.timecode import (
    OPCODE_ARTTIMECODE,
    QUARTER_FRAME,
    MtcScheduler,
    encode_arttimecode,
    full_frame,
    timecode_for,
)

pytestmark = [pytest.mark.requirement("LIGHT-13774")]

TWO_FRAMES_NS = 80_000_000


def test_full_frame_deck_two_25fps() -> None:
    """[if] deck 2 is at 00:01:30.12 at 25 fps [then] the full frame SysEx carries hours 2 with 25 fps rate bits, [else stop]."""
    tc = timecode_for(2, 90 + 12 / 25, 25)
    assert full_frame(tc).hex(" ").upper() == "F0 7F 7F 01 01 22 01 1E 0C F7"
    packet = encode_arttimecode(tc)
    assert int.from_bytes(packet[8:10], "little") == OPCODE_ARTTIMECODE
    assert packet[14:] == bytes([12, 30, 1, 2, 1])


def test_two_frames_give_eight_quarter_frames_in_order() -> None:
    """[if] two frames elapse [then] exactly 8 quarter-frame messages were scheduled in piece order 0..7, [else stop]."""
    sched = MtcScheduler(deck=1)
    sched.seek(10.0, 0)
    msgs = [m for m in sched.advance(TWO_FRAMES_NS) if m.data[0] == QUARTER_FRAME]
    assert [m.data[1] >> 4 for m in msgs] == list(range(8))


def test_seek_puts_full_frame_before_next_quarter_frame() -> None:
    """[if] a seek happens [then] one full frame message precedes the next quarter frame, [else stop]."""
    sched = MtcScheduler(deck=3)
    sched.seek(0.0, 0)
    sched.advance(TWO_FRAMES_NS)
    sched.seek(60.0, TWO_FRAMES_NS)
    msgs = sched.advance(TWO_FRAMES_NS * 2)
    assert msgs[0].data[0] == 0xF0
    assert sum(m.data[0] == 0xF0 for m in msgs) == 1
    assert msgs[1].data[0] == QUARTER_FRAME


def test_29_97_fps_rejected() -> None:
    """[if] 29.97 fps is requested [then] ValueError naming the supported rates, [else stop]."""
    with pytest.raises(ValueError, match="24, 25, 30"):
        timecode_for(1, 0.0, 29.97)
    with pytest.raises(ValueError, match="24, 25, 30"):
        MtcScheduler(deck=1, fps=29.97)  # type: ignore[arg-type]
