"""UDP output loop over loopback sockets (L24, issue #9878)."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from apps.lighting import monitor
from apps.lighting.artnet_poll import OPCODE_ARTPOLL
from apps.lighting.net_out import NetOut
from apps.lighting.settings import NetOutSettingError, NetOutSettings

REPLY_FIXTURE = Path(__file__).parent / "fixtures" / "artpollreply_two_ports.bin"


def _receiver() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


def _drain(sock: socket.socket) -> list[tuple[bytes, tuple[str, int]]]:
    out = []
    while True:
        try:
            out.append(sock.recvfrom(2048))
        except BlockingIOError:
            return out


def _changing(universe: int, tick: int) -> bytes:
    return bytes([tick % 256]) + bytes(511)


async def _run_sacn_with_drain(sock: socket.socket, duration_s: float) -> list[bytes]:
    out = NetOut(
        NetOutSettings(protocol="sacn", universes=(1,), sacn_destination="127.0.0.1", sacn_port=sock.getsockname()[1]),
        _changing,
    )
    received: list[bytes] = []
    task = asyncio.create_task(out.run(asyncio.Event(), duration_s=duration_s))
    while not task.done():
        received += [data for data, _ in _drain(sock)]
        await asyncio.sleep(0.01)
    await task
    await asyncio.sleep(0.05)
    received += [data for data, _ in _drain(sock)]
    return received


def test_sacn_unicast_two_seconds_decodes_80_to_88_frames_with_consecutive_sequences() -> None:
    """[if] sACN unicast to 127.0.0.1 runs 2 s with one changing universe [then] 80..88 frames, consecutive sequences, [else stop]."""
    sock = _receiver()
    try:
        packets = asyncio.run(_run_sacn_with_drain(sock, 2.0))
    finally:
        sock.close()
    frames = [monitor.decode_e131_data(p) for p in packets]
    data = [f for f in frames if not (f.options or 0) & 0x40]
    sequences = sorted({f.sequence for f in data})
    assert 80 <= len(sequences) <= 88
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))


def test_stopping_the_loop_ends_with_exactly_three_terminated_packets() -> None:
    """[if] the loop stops [then] the receiver sees exactly 3 Stream_Terminated packets, all last, [else stop]."""
    sock = _receiver()
    try:
        packets = asyncio.run(_run_sacn_with_drain(sock, 0.3))
    finally:
        sock.close()
    flags = [bool((monitor.decode_e131_data(p).options or 0) & 0x40) for p in packets]
    assert sum(flags) == 3
    assert flags[-3:] == [True, True, True]


def _reply_for(port: int) -> bytes:
    reply = bytearray(REPLY_FIXTURE.read_bytes())
    reply[10:14] = bytes([127, 0, 0, 1])
    reply[14:16] = port.to_bytes(2, "little")
    reply[18] = 0
    reply[19] = 0
    reply[190] = 1  # first output port carries Port-Address 1
    return bytes(reply)


async def _run_artnet(node: socket.socket) -> tuple[list[monitor.DmxFrame], list[tuple[str, int]]]:
    port = node.getsockname()[1]
    out = NetOut(
        NetOutSettings(protocol="artnet", universes=(1,), artnet_poll_address="127.0.0.1", artnet_port=port),
        _changing,
    )
    frames: list[monitor.DmxFrame] = []
    task = asyncio.create_task(out.run(asyncio.Event(), duration_s=0.5))
    while not task.done():
        for data, addr in _drain(node):
            if int.from_bytes(data[8:10], "little") == OPCODE_ARTPOLL:
                node.sendto(_reply_for(port), addr)
            else:
                frames.append(monitor.decode_artdmx(data))
        await asyncio.sleep(0.005)
    await task
    return frames, out.sent_to


def test_artnet_sends_artdmx_unicast_to_the_polled_node_and_never_to_a_broadcast() -> None:
    """[if] Art-Net is selected and a fake 127.0.0.1 node answered ArtPoll for universe 1 [then] ArtDmx arrives unicast, no broadcast, [else stop]."""
    node = _receiver()
    try:
        frames, sent_to = asyncio.run(_run_artnet(node))
    finally:
        node.close()
    assert frames and all(f.universe == 1 for f in frames)
    assert {host for host, _ in sent_to} == {"127.0.0.1"}
    assert not any(host.endswith(".255") for host, _ in sent_to)


def test_start_without_a_destination_fails_naming_the_missing_setting() -> None:
    """[if] settings name no destination [then] start fails with an error naming the missing setting, [else stop]."""
    with pytest.raises(NetOutSettingError, match=r"lighting\.net_out\.sacn_destination"):
        NetOut(NetOutSettings(protocol="sacn", universes=(1,)), _changing)
    with pytest.raises(NetOutSettingError, match=r"lighting\.net_out\.artnet_poll_address"):
        NetOut(NetOutSettings(protocol="artnet", universes=(1,)), _changing)
