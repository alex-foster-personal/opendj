"""B2BBOOTH-123 on the ENGINE: a deck op sent while this DJ's grab of that deck waits is held until the
grab resolves, then committed once, right after the grant (and the load it was asked for), or dropped
when the grab ends refused or locked. Double-click load-and-play onto the partner's deck then plays.

- [if] A loads AND plays a deck B holds, B silent [then] nothing but the ask commits before 1 s; then grant,
  load, play in that order, the play's at_link_us moved to the grant time plus the lead it was sent with
- [if] B yields [then] grant, load, play at once; [if] B refuses [then] neither the load nor the play ever commits
- [if] the turn lock ends the ask locked [then] the held play is dropped
- [if] the joiner does it on the inviter's deck [then] the same (the joiner proposes the play, no pre-refusal)
- [if] the holder releases while the ask waits [then] the release, then the grant, load and play
- [if] two ops are held [then] both commit after the grant, in the order sent
- [if] play on a partner deck with NO grab of yours waiting [then] still not_holder (overshoot control)

[if] a held load-and-play commits before grab grant or after grab refusal [then] fail, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.b2b_booth.lease import GRAB_FALLBACK_US, RENEW_INTERVAL_US
from tests.b2b_booth.booth_pair import T0, Pair, same_booth

pytestmark = [pytest.mark.requirement("B2BBOOTH-123")]

TRACK = {"sha256": "ef" * 32, "bytes": 3000, "title": "Dbl", "artist": "A", "bpm": 125.0, "key": "4A",
         "duration_s": 200.0, "owner_track_id": "rb-9"}
LEAD = 120_000


def _load(deck: int) -> dict[str, Any]:
    return {"op": "load", "deck": deck, "track": TRACK}


def _play(deck: int, now: int) -> dict[str, Any]:
    return {"op": "play", "deck": deck, "at_link_us": now + LEAD, "pos_frames": 0}


def _names(applied: list[dict[str, Any]]) -> list[str]:
    return [c["op"]["op"] + (f":{c['op']['phase']}" if "phase" in c["op"] else "") for c in applied]


def _renewing(pair: Pair, side: str, until: int) -> None:
    for t in range(T0, until + 1, RENEW_INTERVAL_US):
        pair.renew(side, t)


def _playing(pair: Pair, deck: int) -> tuple[bool, bool]:
    return pair.inviter.state.decks[deck]["playing"], pair.joiner.state.decks[deck]["playing"]


def test_load_and_play_on_a_held_deck_waits_for_the_grant_then_plays_with_its_lead() -> None:
    pair = Pair()
    t = T0 + 400_000
    _renewing(pair, "joiner", t)
    assert _names(pair.act("inviter", _load(3), t)) == ["grab:ask"]
    assert pair.act("inviter", _play(3, t + 5_000), t + 5_000) == [], "if the play commits before the grant then broken"
    assert pair.tick(t + GRAB_FALLBACK_US - 1) == []
    granted = pair.tick(t + GRAB_FALLBACK_US)
    assert _names(granted) == ["grab:granted", "load", "play"]
    assert granted[2]["op"]["at_link_us"] == t + GRAB_FALLBACK_US + LEAD, "the play keeps its lead from the grant time"
    assert _playing(pair, 3) == (True, True)
    assert same_booth(pair.inviter, pair.joiner)


def test_yield_releases_the_held_play_at_once() -> None:
    pair = Pair()
    _renewing(pair, "joiner", T0 + 200_000)
    pair.act("inviter", _load(4), T0 + 200_000)
    pair.act("inviter", _play(4, T0 + 210_000), T0 + 210_000)
    assert _names(pair.act("joiner", {"op": "yield", "unit": "deck:4"}, T0 + 300_000)) == ["grab:granted", "load", "play"]


def test_a_refused_grab_drops_the_held_load_and_play_for_good() -> None:
    pair = Pair()
    _renewing(pair, "joiner", T0 + 200_000)
    pair.act("inviter", _load(3), T0 + 200_000)
    pair.act("inviter", _play(3, T0 + 210_000), T0 + 210_000)
    assert _names(pair.act("joiner", {"op": "refuse", "unit": "deck:3"}, T0 + 400_000)) == ["refuse"]
    assert pair.tick(T0 + 5 * GRAB_FALLBACK_US) == []
    assert _playing(pair, 3) == (False, False) and pair.inviter.state.decks[3]["track"] is None
    assert pair.inviter.arbiter is not None and pair.inviter.arbiter.held_dropped == 2


def test_a_locked_ending_drops_the_held_play() -> None:
    pair = Pair()
    pair.act("inviter", _load(3) | {"deck": 1}, T0 + 1)  # control: the inviter's own deck loads at once
    pair.act("joiner", {"op": "onair", "side": "joiner"}, T0 + 2)
    _renewing(pair, "joiner", T0 + 100_000)
    assert _names(pair.act("inviter", _load(3), T0 + 100_000)) == ["grab:ask"]
    pair.act("inviter", _play(3, T0 + 110_000), T0 + 110_000)
    pair.act("joiner", {"op": "setting", "side": "joiner", "lock_half_on_turn": True}, T0 + 120_000)
    applied = pair.tick(T0 + 130_000)
    assert "play" not in _names(applied) and "load" not in _names(applied)
    assert pair.tick(T0 + 5 * GRAB_FALLBACK_US) == []
    assert _playing(pair, 3) == (False, False)
    assert pair.inviter.arbiter is not None and pair.inviter.arbiter.held_dropped == 2


def test_the_joiner_double_clicks_onto_the_inviters_deck() -> None:
    pair = Pair()
    _renewing(pair, "inviter", T0 + 100_000)
    assert _names(pair.act("joiner", _load(1), T0 + 100_000)) == ["grab:ask"]
    out = pair.joiner.intent_op(_play(1, T0 + 110_000), T0 + 110_000)
    assert out.refused is None, "if the joiner pre-refuses the play while its own grab waits then broken"
    pair.flush("joiner", out, T0 + 110_000)
    granted = pair.tick(T0 + 100_000 + GRAB_FALLBACK_US)
    assert _names(granted) == ["grab:granted", "load", "play"]
    assert all(c["by"] == "joiner" for c in granted)


def test_a_release_by_the_holder_hands_the_deck_over_with_the_held_ops() -> None:
    pair = Pair()
    _renewing(pair, "joiner", T0 + 100_000)
    pair.act("inviter", _load(3), T0 + 100_000)
    pair.act("inviter", _play(3, T0 + 110_000), T0 + 110_000)
    applied = pair.act("joiner", {"op": "release", "unit": "deck:3"}, T0 + 200_000)
    assert _names(applied) == ["release", "grab:granted", "load", "play"]
    assert pair.joiner.state.holders["deck:3"] == "inviter"


def test_two_held_ops_commit_in_the_order_sent() -> None:
    pair = Pair()
    _renewing(pair, "joiner", T0 + 100_000)
    pair.act("inviter", _load(3), T0 + 100_000)
    pair.act("inviter", {"op": "cue", "deck": 3, "at_link_us": T0 + 110_000, "pos_frames": 480}, T0 + 110_000)
    pair.act("inviter", _play(3, T0 + 120_000), T0 + 120_000)
    assert _names(pair.tick(T0 + 100_000 + GRAB_FALLBACK_US)) == ["grab:granted", "load", "cue", "play"]


def test_play_on_a_partner_deck_with_no_grab_waiting_is_still_not_holder() -> None:
    pair = Pair()
    assert pair.inviter.intent_op(_play(3, T0), T0 + 1).refused == "not_holder"
    assert pair.joiner.intent_op(_play(1, T0), T0 + 1).refused == "not_holder"
    _renewing(pair, "joiner", T0 + 100_000)
    pair.act("inviter", _load(3), T0 + 100_000)
    pair.act("joiner", {"op": "refuse", "unit": "deck:3"}, T0 + 200_000)
    assert pair.inviter.intent_op(_play(3, T0 + 300_000), T0 + 300_000).refused == "not_holder", (
        "after the grab ended refused, a play is refused again, never held forever")


def test_the_joiner_forgets_its_grab_once_the_log_resolves_it() -> None:
    """After the inviter refuses the joiner's load grab, a joiner play is pre-refused again (no proposal)."""
    pair = Pair()
    _renewing(pair, "inviter", T0 + 100_000)
    pair.act("joiner", _load(1), T0 + 100_000)
    pair.act("inviter", {"op": "refuse", "unit": "deck:1"}, T0 + 200_000)
    out = pair.joiner.intent_op(_play(1, T0 + 300_000), T0 + 300_000)
    assert (out.refused, out.ops) == ("not_holder", []), "if the joiner keeps proposing on a resolved grab then broken"
