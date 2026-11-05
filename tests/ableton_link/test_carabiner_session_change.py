"""LINK-118: the Carabiner client tells its owner the moment the session tempo or timeline moves.

Real ``CarabinerClient._handle`` fed real Carabiner status lines. ``_handle`` is the reader
thread's per-line step, so the calls it makes are the calls the reader makes. The owner (the
link route) turns each call into one ``link.changed`` invalidation, and the page re-reads the
clock at once instead of at its next 4 Hz poll (ADR: ADR-0177-link-session-push).

- [if] a tempo change on a status line does not call the hook [then] broken (the follower waits for its poll)
- [if] a timeline move (a forced beat moves ``:start``) does not call the hook [then] broken
- [if] Carabiner's 1 s liveness answer (same tempo, same start) calls the hook [then] broken (an
  invalidation per second per laptop, every one a wasted clock read)
- [if] ``:start`` re-rounded by under a millisecond calls the hook [then] broken (Link re-measures
  its host-time mapping all the time; that is not a session move)
- [if] the hook runs while the client holds its lock [then] broken (a hook that reads the
  snapshot would deadlock the reader)
- [if] a hook that raises takes the reader down [then] broken, [else stop]
"""

from __future__ import annotations

import pytest

from apps.ableton_link.carabiner import CarabinerClient

pytestmark = [pytest.mark.requirement("LINK-118")]

START_US = 73746356220


def _status(bpm: float = 128.0, start_us: float = START_US, peers: int = 1) -> str:
    return f"status {{ :peers {peers} :bpm {bpm:.6f} :start {int(start_us)} :beat 597.737570 }}"


class _Hook:
    def __init__(self, client_box: list[CarabinerClient]) -> None:
        self.calls = 0
        self._box = client_box

    def __call__(self) -> None:
        # Reading the snapshot from inside the hook must not deadlock: the hook runs unlocked.
        assert self._box[0].snapshot().bpm is not None
        self.calls += 1


def _client_with_hook() -> tuple[CarabinerClient, _Hook]:
    box: list[CarabinerClient] = []
    hook = _Hook(box)
    client = CarabinerClient(port=17000, on_session_change=hook)
    box.append(client)
    return client, hook


def test_first_status_tempo_change_and_forced_beat_each_call_the_hook_once() -> None:
    client, hook = _client_with_hook()
    client._handle(_status())
    assert hook.calls == 1, f"if the first session state does not call the hook then broken: {hook.calls}"
    client._handle(_status(bpm=130.56))
    assert hook.calls == 2, f"if a tempo change does not call the hook then broken: {hook.calls}"
    client._handle(_status(bpm=130.56, start_us=START_US + 250_000))
    assert hook.calls == 3, f"if a forced beat (start moved 250 ms) does not call the hook then broken: {hook.calls}"


def test_liveness_answers_and_sub_millisecond_start_rounding_do_not_call_the_hook() -> None:
    client, hook = _client_with_hook()
    client._handle(_status())
    for _ in range(5):  # Carabiner's 1 s liveness answers: same tempo, same timeline
        client._handle(_status())
    client._handle(_status(start_us=START_US + 400))  # 0.4 ms: Link re-measured its host mapping
    client._handle(_status(peers=0))  # a peer count change is not a tempo or timeline move
    assert hook.calls == 1, f"if a status line with the same session calls the hook then broken: {hook.calls}"


def test_a_raising_hook_is_reported_and_the_reader_keeps_its_state(caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> None:
        raise RuntimeError("publish failed")

    client = CarabinerClient(port=17000, on_session_change=boom)
    client._handle(_status())
    client._handle(_status(bpm=122.0))
    snap = client.snapshot()
    assert snap.bpm == 122.0 and snap.error is None, f"if a raising hook stops the reader's state then broken: {snap}"
    assert "publish failed" in caplog.text, "if a raising hook is swallowed silently then broken"


def test_no_hook_is_the_default() -> None:
    client = CarabinerClient(port=17000)
    client._handle(_status())
    client._handle(_status(bpm=122.0))
    assert client.snapshot().bpm == 122.0
