"""OS2L beat strength from bar energy and btn messages on section changes, against a loopback server.

[if] a profile is attached to the followed deck [then] beats carry strength and section changes send btn, [else stop].
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.lighting.os2l_client import Os2lClient, load_buttons
from tests.lighting.test_os2l_client import BPM, Server, VirtualClock, beat_msg, run_client, stop, wait_for

pytestmark = [pytest.mark.asyncio, pytest.mark.requirement("LIGHT-13817")]


@dataclass(frozen=True)
class FakeSection:
    label: str
    start_bar: int
    bars: int


@dataclass(frozen=True)
class FakeProfile:
    bar_energy: tuple[float, ...]
    sections: tuple[FakeSection, ...] = ()


async def _exchange(profile: FakeProfile | None, bars: list[int]) -> list[dict]:
    server = Server()
    port = await server.start()
    clock = VirtualClock()
    client = Os2lClient("127.0.0.1", port, clock=clock, sleep=clock.sleep)
    client.on_message({"type": "deck_weights", "weights": {"1": 1.0}})
    client.set_profile("1", profile)
    for n, bar in enumerate(bars):
        client.on_message({**beat_msg("1", n), "bar": bar})
    task = await run_client(client)
    expected = client._queue.qsize()
    await wait_for(lambda: len(server.received) == expected)
    await stop(task)
    await server.close()
    return [m for _, m in server.received]


async def test_bar_energy_sets_strength() -> None:
    """[if] a bar has energy 0.73 [then] its beats carry strength 73, [else stop]."""
    received = await _exchange(FakeProfile(bar_energy=(0.2, 0.73)), [2, 2, 2, 2])
    beats = [m for m in received if m["evt"] == "beat"]
    assert len(beats) == 4
    assert all(b["strength"] == 73 and b["bpm"] == BPM for b in beats)


async def test_no_profile_omits_strength() -> None:
    """[if] no profile is available [then] beat messages carry no strength key at all, [else stop]."""
    received = await _exchange(None, [1, 1, 2])
    assert len(received) == 3
    assert all(m["evt"] == "beat" and "strength" not in m for m in received)


async def test_build_to_drop_sends_btn_off_then_on() -> None:
    """[if] the followed deck goes from build to drop [then] the server gets btn build off then btn drop on before the next beat, [else stop]."""
    profile = FakeProfile(
        bar_energy=(0.5, 0.9),
        sections=(FakeSection("build", 1, 1), FakeSection("drop", 2, 1)),
    )
    received = await _exchange(profile, [1, 2])
    shape = [(m["evt"], m.get("name"), m.get("state")) for m in received]
    assert shape == [
        ("btn", "build", "on"),
        ("beat", None, None),
        ("btn", "build", "off"),
        ("btn", "drop", "on"),
        ("beat", None, None),
    ]


async def test_button_table_is_data() -> None:
    """[if] the shipped button table loads [then] it maps build and drop to button names, [else stop]."""
    table = load_buttons()
    assert table["build"] == "build" and table["drop"] == "drop"
