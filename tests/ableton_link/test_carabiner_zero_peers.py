"""B2BMCAST-01: Carabiner's own continuous 0-peer streak (``LinkSnapshot.zero_peers_for_s``).

Real ``CarabinerClient._handle`` fed real Carabiner status lines (the format in the module
docstring), on a monkeypatched monotonic clock. No sockets: ``_handle`` is the reader
thread's per-line step, so the streak it keeps is the one the reader keeps.

- [if] the streak does not start on the first ``:peers 0`` line [then] broken
- [if] a repeated 0 line restarts the streak (so whoever polls resets the hold) [then] broken
- [if] a line with peers > 0 does not clear it to None [then] broken
- [if] connected_for_s is not measured from the first status line, or moves on a new 0 streak [then] broken
- [if] Carabiner reports 0 peers line after line [then] one streak runs from the first 0 until peers return, [else stop]
"""

from __future__ import annotations

import types

import pytest

from apps.ableton_link import carabiner
from apps.ableton_link.carabiner import CarabinerClient

pytestmark = [pytest.mark.requirement("B2BMCAST-01")]


def _status(peers: int) -> str:
    return f"status {{ :peers {peers} :bpm 128.000000 :start 73746356220 :beat 597.737570 }}"


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    # Replace only carabiner's view of ``time`` (not the global module other threads use).
    monkeypatch.setattr(carabiner, "time", types.SimpleNamespace(monotonic=fake.monotonic))
    return fake


def test_zero_peer_streak_starts_runs_resets_and_restarts(clock: _Clock) -> None:
    client = CarabinerClient(port=17000)
    assert client.snapshot().zero_peers_for_s is None and client.snapshot().connected_for_s is None, (
        "if a client with no status line reports a duration then broken"
    )

    client._handle(_status(0))
    snap = client.snapshot()
    assert snap.peers == 0 and snap.zero_peers_for_s == 0.0, f"if the streak does not start on the first 0 line then broken: {snap}"
    assert snap.connected_for_s == 0.0, f"if connected_for_s does not start on the first line then broken: {snap}"

    for step in (1.0, 2.0, 3.5):  # Carabiner's 1 s liveness polls keep answering 0
        clock.now = 500.0 + step
        client._handle(_status(0))
    clock.now = 504.0
    snap = client.snapshot()
    assert snap.zero_peers_for_s == 4.0, f"if repeated 0 lines restart the streak then broken: {snap}"
    assert snap.connected_for_s == 4.0

    clock.now = 505.0
    client._handle(_status(1))
    snap = client.snapshot()
    assert snap.peers == 1 and snap.zero_peers_for_s is None, f"if peers > 0 does not clear the streak then broken: {snap}"

    clock.now = 507.0
    client._handle(_status(0))
    clock.now = 509.5
    snap = client.snapshot()
    assert snap.zero_peers_for_s == 2.5, f"if a new 0 after a peer does not restart the streak from that line then broken: {snap}"
    assert snap.connected_for_s == 9.5, f"if connected_for_s moves with the 0 streak then broken: {snap}"


def test_non_status_and_malformed_lines_do_not_start_either_clock(clock: _Clock) -> None:
    client = CarabinerClient(port=17000)
    client._handle("version 1.2.0")
    client._handle("bad-bpm 9999")
    snap = client.snapshot()
    assert snap.connected_for_s is None and snap.zero_peers_for_s is None, (
        f"if a non-status line starts connected_for_s then broken: {snap}"
    )
    client._handle("status { :bpm 120.0 }")  # no :peers -> ValueError -> recorded error
    assert client.snapshot().error is not None and client.snapshot().zero_peers_for_s is None
    clock.now = 501.0
    client._handle(_status(0))
    clock.now = 503.0
    snap = client.snapshot()
    assert snap.connected_for_s == 2.0 and snap.zero_peers_for_s == 2.0, f"if the first real status line is not the origin then broken: {snap}"


def test_a_malformed_line_mid_streak_does_not_break_it(clock: _Clock) -> None:
    client = CarabinerClient(port=17000)
    client._handle(_status(0))
    clock.now = 502.0
    client._handle("status { :peers x :bpm 120.0 }")
    clock.now = 504.0
    client._handle(_status(0))
    assert client.snapshot().zero_peers_for_s == 4.0, "if an unparseable line silently restarts the 0 streak then broken"
