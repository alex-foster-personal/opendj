"""LIGHT-13806: OS2L client tests against an asyncio TCP server on 127.0.0.1.

[if] the OS2L client follows a beat source [then] the loopback server receives one beat per beat, offset and reconnect hold, [else stop].
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from apps.lighting.os2l_client import Os2lClient

BPM = 124.0
BEAT_NS = round(60_000_000_000 / BPM)


class VirtualClock:
    """Virtual time: sleep advances the clock and yields, so scheduling is measured exactly."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0, round(seconds * 1e9))
        await asyncio.sleep(0)


class Server:
    def __init__(self) -> None:
        self.received: list[tuple[int, dict]] = []
        self.conn = 0
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.conn += 1
        conn = self.conn
        self.writers.append(writer)
        buf = b""
        while chunk := await reader.read(4096):
            buf += chunk
            text = buf.decode()
            dec = json.JSONDecoder()
            idx = 0
            while idx < len(text):
                obj, idx = dec.raw_decode(text, idx)
                self.received.append((conn, obj))
            buf = b""

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        for w in self.writers:
            w.close()


def beat_msg(deck: str, n: int, base_ns: int = 0) -> dict:
    return {"type": "beat", "deck": deck, "beat_index": n, "bpm": BPM, "at_host_ns": base_ns + n * BEAT_NS}


async def wait_for(pred, timeout: float = 2.0) -> None:
    async def loop() -> None:
        while not pred():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(loop(), timeout)


async def run_client(client: Os2lClient):
    return asyncio.create_task(client.run())


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_replayed_trace_sends_eight_beats() -> None:
    """[if] a replayed trace plays 8 beats at 124 BPM [then] the server gets 8 beats, pos +1, bpm 124.0, [else stop]."""
    server = Server()
    port = await server.start()
    clock = VirtualClock()
    client = Os2lClient("127.0.0.1", port, clock=clock, sleep=clock.sleep)
    client.on_message({"type": "deck_weights", "weights": {"1": 1.0}})
    for n in range(8):
        client.on_message(beat_msg("1", n))
    task = await run_client(client)
    await wait_for(lambda: len(server.received) == 8)
    await stop(task)
    await server.close()
    beats = [m for _, m in server.received]
    assert [b["pos"] for b in beats] == list(range(1, 9))
    assert all(b["evt"] == "beat" and b["bpm"] == 124.0 for b in beats)


async def _send_times(offset_ms: float) -> list[int]:
    server = Server()
    port = await server.start()
    clock = VirtualClock()
    client = Os2lClient("127.0.0.1", port, offset_ms=offset_ms, clock=clock, sleep=clock.sleep)
    for n in range(8):
        client.on_message(beat_msg("1", n, base_ns=1_000_000_000))
    task = await run_client(client)
    await wait_for(lambda: len(server.received) == 8)
    await stop(task)
    await server.close()
    return [t for t, _ in client.sent]


async def test_negative_offset_sends_earlier() -> None:
    """[if] the offset is -20 ms [then] each message is sent 20 ms earlier than with offset 0 within 3 ms, [else stop]."""
    base = await _send_times(0.0)
    early = await _send_times(-20.0)
    for a, b in zip(base, early, strict=True):
        assert abs((a - b) - 20_000_000) <= 3_000_000


async def test_reconnects_after_server_close() -> None:
    """[if] the server closes the socket [then] the client reconnects within 1 s and the next beat uses it, [else stop]."""
    server = Server()
    port = await server.start()
    client = Os2lClient("127.0.0.1", port)
    task = await run_client(client)
    now = asyncio.get_running_loop().time
    client.on_message(beat_msg("1", 0, base_ns=time.monotonic_ns() - 2 * BEAT_NS))
    await wait_for(lambda: len(server.received) == 1)
    for w in server.writers:
        w.close()
    closed_at = now()
    await wait_for(lambda: server.conn == 2)
    assert now() - closed_at < 1.0
    client.on_message(beat_msg("1", 1, base_ns=time.monotonic_ns() - 2 * BEAT_NS))
    await wait_for(lambda: len(server.received) == 2)
    await stop(task)
    await server.close()
    assert server.received[1][0] == 2


async def test_followed_deck_change_flags_one_beat() -> None:
    """[if] the followed deck changes from 1 to 2 [then] exactly one beat carries change true, [else stop]."""
    server = Server()
    port = await server.start()
    clock = VirtualClock()
    client = Os2lClient("127.0.0.1", port, clock=clock, sleep=clock.sleep)
    client.on_message({"type": "deck_weights", "weights": {"1": 1.0, "2": 0.0}})
    for n in range(4):
        client.on_message(beat_msg("1", n))
        client.on_message(beat_msg("2", n))
    client.on_message({"type": "deck_weights", "weights": {"1": 0.2, "2": 0.8}})
    for n in range(4, 8):
        client.on_message(beat_msg("1", n))
        client.on_message(beat_msg("2", n))
    task = await run_client(client)
    await wait_for(lambda: len(server.received) == 8)
    await stop(task)
    await server.close()
    changes = [m for _, m in server.received if m["change"]]
    assert len(changes) == 1


pytestmark = [pytest.mark.asyncio, pytest.mark.requirement("LIGHT-13806")]
