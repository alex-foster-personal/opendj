"""LIGHT-13757: ENTTEC DMX USB Pro framing and a serial writer tested on a pseudo-terminal.

[if] a frame is not 0x7E, label, length, data, 0xE7 or a pty round trip changes bytes [then] fail, [else stop].
"""

from __future__ import annotations

import os
import tty

import pytest

from apps.lighting.enttec_pro import FdSink, ProWriter, dmx_frame, parse_message, parse_params_reply, set_params_frame

pytestmark = [pytest.mark.requirement("LIGHT-13757")]


def test_three_slots_pad_to_25_bytes_and_end_with_e7() -> None:
    """[if] 3 slots are framed with label 6 [then] the payload is padded to 25 bytes and ends with 0xE7."""
    msg = dmx_frame(b"\x01\x02\x03")
    assert msg[:4] == bytes([0x7E, 6, 25, 0])
    assert msg[4:29] == b"\x00\x01\x02\x03" + bytes(21)
    assert msg[-1] == 0xE7 and len(msg) == 30


def test_parameters_reply_parses_break_mab_rate() -> None:
    """[if] a parameters reply with known bytes is parsed [then] break, MAB and rate match the document's units."""
    reply = bytes([0x7E, 3, 5, 0, 0x2C, 0x01, 9, 1, 40, 0xE7])
    p = parse_params_reply(reply)
    assert p.firmware == 0x012C
    assert (p.break_time, p.mab_time, p.rate) == (9, 1, 40)
    assert p.break_us == pytest.approx(96.03)
    assert p.mab_us == pytest.approx(10.67)


def test_rate_41_is_rejected() -> None:
    """[if] rate 41 is requested [then] ValueError (range 0..40)."""
    with pytest.raises(ValueError):
        set_params_frame(9, 1, 41)
    assert set_params_frame(9, 1, 40)[4:9] == bytes([0, 0, 9, 1, 40])


def test_frames_round_trip_through_a_pty() -> None:
    """[if] frames are written to a pty [then] the reader on the other end reassembles them byte-identical."""
    master, slave = os.openpty()
    try:
        tty.setraw(slave)
        frames = [bytes(range(i, i + 30)) for i in (0, 50, 100)]
        writer = ProWriter(FdSink(master))
        for f in frames:
            writer.send_dmx(f)
        expected = b"".join(dmx_frame(f) for f in frames)
        buf = b""
        while len(buf) < len(expected):
            buf += os.read(slave, 4096)
        assert buf == expected
        got = []
        while (m := parse_message(buf)) is not None:
            label, data, buf = m
            got.append((label, data))
        assert got == [(6, b"\x00" + f) for f in frames]
    finally:
        os.close(master)
        os.close(slave)
