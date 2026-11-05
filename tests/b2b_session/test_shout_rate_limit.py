"""LINKSHOUT-01: the engine rate-limits outgoing shouts (FE-logic audit #6646: none existed, so an
agent loop could flood the partner's screen).

- [if] shouts are sent faster than the bucket allows [then] the next is refused with how long to
  wait, and nothing reaches the outbox [else the partner is flooded]
- [if] the bucket refills [then] one shout per refill period is accepted again (control)
- [if] replies are sent [then] they are never limited (they answer shouts that already arrived)
- [if] a malformed shout is sent [then] it is refused as malformed and spends no token

[if] shout rate limiting regresses [then] fail naming the refusal, [else stop].
"""

from __future__ import annotations

import pytest

from apps.b2b_session import b2b_shout

pytestmark = [pytest.mark.requirement("LINKSHOUT-01")]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _outbox(clock: _Clock) -> b2b_shout.ShoutOutbox:
    return b2b_shout.ShoutOutbox(machine="maintainer", clock=clock)


def test_a_burst_past_the_bucket_is_refused_with_the_wait_and_never_queued() -> None:
    clock = _Clock()
    box = _outbox(clock)
    for _ in range(b2b_shout.SHOUT_BURST):
        box.shout("sick", None)
    with pytest.raises(b2b_shout.ShoutRateLimited) as refused:
        box.shout("sick", None)
    assert 0 < refused.value.retry_after_s <= b2b_shout.SHOUT_REFILL_S
    assert len(box.events) == b2b_shout.SHOUT_BURST, "a refused shout never reaches the outbox"


def test_the_bucket_refills_one_shout_per_period() -> None:
    clock = _Clock()
    box = _outbox(clock)
    for _ in range(b2b_shout.SHOUT_BURST):
        box.shout("sick", None)
    clock.now += b2b_shout.SHOUT_REFILL_S
    box.shout("sick", None)
    with pytest.raises(b2b_shout.ShoutRateLimited):
        box.shout("sick", None)
    clock.now += 10 * b2b_shout.SHOUT_REFILL_S
    for _ in range(b2b_shout.SHOUT_BURST):
        box.shout("sick", None)  # refills to the burst, never past it
    with pytest.raises(b2b_shout.ShoutRateLimited):
        box.shout("sick", None)


def test_replies_are_never_limited() -> None:
    clock = _Clock()
    box = _outbox(clock)
    for _ in range(b2b_shout.SHOUT_BURST):
        box.shout("sick", None)
    for _ in range(3 * b2b_shout.SHOUT_BURST):
        box.reply("x", "up")


def test_a_malformed_shout_spends_no_token() -> None:
    box = _outbox(_Clock())
    for _ in range(3 * b2b_shout.SHOUT_BURST):
        with pytest.raises(ValueError):
            box.shout("next_track", None)  # needs a track: refused as malformed, never as a flood
    for _ in range(b2b_shout.SHOUT_BURST):
        box.shout("sick", None)
