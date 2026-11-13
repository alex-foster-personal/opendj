"""Output audit over captured packets: cadence, sequence, termination and addressing.

[if] a captured stream breaks output cadence, sequence, termination or addressing rules [then] flag it, [else stop].
"""

from __future__ import annotations

import uuid

import pytest

from apps.lighting.artnet import encode_artdmx, next_sequence
from apps.lighting.output_audit import CapturedPacket, audit
from apps.lighting.sacn import SacnFrame, encode_data_packet

pytestmark = [pytest.mark.requirement("LIGHT-14441")]

CID = uuid.UUID(int=1)
HOST = "192.168.1.20"
DESTS = {1: [HOST]}


def _sacn(at: float, value: int, seq: int, *, terminate: bool = False) -> CapturedPacket:
    frame = SacnFrame(
        cid=CID, source_name="t", universe=1, slots=bytes([value]), sequence=seq % 256, terminate=terminate
    )
    return CapturedPacket(at, "239.255.0.1", encode_data_packet(frame))


def _stop(at: float, seq: int) -> list[CapturedPacket]:
    return [_sacn(at, 0, seq + i, terminate=True) for i in range(3)]


def _codes(packets: list[CapturedPacket], dests: dict[int, list[str]] = DESTS) -> list[str]:
    return [finding.code for finding in audit(packets, dests)]


def _active(fps: float, start: float = 0.0, n: int = 20) -> list[CapturedPacket]:
    return [_sacn(start + i / fps, i % 256, i) for i in range(n)]


def test_cadence_active_and_idle() -> None:
    """[if] changing data runs outside 40..44 fps or idle repeats fall outside 800..1000 ms [then] flag, [else stop]."""
    good = _active(42.0)
    good += [_sacn(good[-1].at + 0.9 * k, 19, 19 + k) for k in (1, 2)]
    good += _stop(good[-1].at + 0.01, 22)
    assert _codes(good) == []
    fast = _active(60.0)
    assert "cadence_active" in _codes(fast + _stop(1.0, 20))
    slow_idle = [*_active(42.0), _sacn(0.5 + 1.2, 19, 20)]
    assert _codes(slow_idle + _stop(2.0, 21)) == ["cadence_idle"]


def test_sequence_consecutive_per_universe() -> None:
    """[if] a universe's sequence numbers skip [then] flag sequence_gap, [else stop]."""
    stream = _active(42.0) + _stop(1.0, 20)
    assert _codes(stream) == []
    gapped = [*stream[:5], *[_sacn(p.at, i + 5, i + 7) for i, p in enumerate(stream[5:20])], *_stop(1.0, 22)]
    assert _codes(gapped) == ["sequence_gap"]
    seq, art = 1, []
    for i in range(300):
        art.append(CapturedPacket(i / 42.0, HOST, encode_artdmx(1, bytes([i % 256, 0]), seq)))
        seq = next_sequence(seq)
    assert _codes(art) == []


def test_exactly_three_terminate_packets() -> None:
    """[if] an sACN stream stops with other than 3 terminate packets [then] flag terminate_count, [else stop]."""
    stream = _active(42.0)
    assert _codes(stream + _stop(1.0, 20)) == []
    assert _codes(stream + _stop(1.0, 20)[:2]) == ["terminate_count"]
    assert _codes(stream + _stop(1.0, 20) + [_sacn(1.01, 0, 23, terminate=True)]) == ["terminate_count"]
    assert _codes(stream) == ["terminate_count"]


def test_flags_broadcast_artdmx_and_unconfigured_universe() -> None:
    """[if] ArtDmx goes to a broadcast address or a universe has no destination [then] flag it, [else stop]."""
    packet = encode_artdmx(1, bytes(2), 1)
    assert _codes([CapturedPacket(0.0, HOST, packet)]) == []
    assert _codes([CapturedPacket(0.0, "255.255.255.255", packet)]) == ["artdmx_broadcast"]
    assert _codes([CapturedPacket(0.0, "2.255.255.255", packet)]) == ["artdmx_broadcast"]
    assert _codes([CapturedPacket(0.0, HOST, encode_artdmx(7, bytes(2), 1))]) == ["no_destination"]
    assert _codes([CapturedPacket(0.0, HOST, packet)], {1: []}) == ["no_destination"]
