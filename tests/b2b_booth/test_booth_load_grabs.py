"""B2BBOOTH-120 on the ENGINE: loading a track onto a deck this DJ does not hold grabs it first.

Product model (the maintainer, Tue 20 Oct 2026): "anyone grabs any deck by default", and each DJ loads from
their OWN library onto ANY deck. So a ``load`` on a deck the other DJ holds is a grab with the load
attached: granted at once when the deck is unheld or its lease lapsed, otherwise an ask the holder
can yield or refuse within 1 s (silence grants it); the load commits right after the grant, and
never without it. Every other deck op still needs the deck held (the overshoot control).

- [if] A loads a deck B holds and B stays silent [then] grab ask, then at 1 s granted + load on both, never before
- [if] B yields [then] granted + load at once; [if] B refuses [then] refused and no load
- [if] the deck's lease lapsed [then] granted + load in the same decision
- [if] the joiner loads the inviter's deck [then] the same, through the inviter's arbiter
- [if] the turn lock covers the deck [then] refused ``locked``, nothing committed
- [if] the loader already holds the deck [then] only the load commits (no grab)
- [if] play (not load) on a deck this DJ does not hold [then] still not_holder
- [if] a second load arrives while the first ask waits [then] already_asked

[if] a load onto a partner-held deck skips grab or commits without grant [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.b2b_booth.lease import GRAB_FALLBACK_US, LEASE_US, RENEW_INTERVAL_US
from tests.b2b_booth.booth_pair import T0, Pair, same_booth

pytestmark = [pytest.mark.requirement("B2BBOOTH-120")]

TRACK = {"sha256": "cd" * 32, "bytes": 2000, "title": "Mine", "artist": "A", "bpm": 126.0, "key": "9A",
         "duration_s": 240.0, "owner_track_id": "rb-7"}


def _load(deck: int) -> dict[str, object]:
    return {"op": "load", "deck": deck, "track": TRACK}


def _names(applied: list[dict[str, object]]) -> list[tuple[str, str | None]]:
    return [(c["op"]["op"], c["op"].get("phase")) for c in applied]  # type: ignore[index, union-attr]


def _keep_renewing(pair: Pair, side: str, until: int) -> None:
    for t in range(T0, until + 1, RENEW_INTERVAL_US):
        pair.renew(side, t)


def _track_on(pair: Pair, deck: int) -> tuple[object, object]:
    return pair.inviter.state.decks[deck]["track"], pair.joiner.state.decks[deck]["track"]


def test_load_on_a_held_deck_asks_then_silence_grants_and_loads_never_before() -> None:
    pair = Pair()
    _keep_renewing(pair, "joiner", T0 + 400_000)
    asked = pair.act("inviter", _load(3), T0 + 400_000)
    assert _names(asked) == [("grab", "ask")], "if the load commits before the grab then broken"
    assert _track_on(pair, 3) == (None, None)
    assert pair.tick(T0 + 400_000 + GRAB_FALLBACK_US - 1) == [], "if it is granted before 1 s then broken"
    granted = pair.tick(T0 + 400_000 + GRAB_FALLBACK_US)
    assert _names(granted) == [("grab", "granted"), ("load", None)]
    assert granted[0]["op"]["reason"] == "silence"
    assert _track_on(pair, 3) == (TRACK, TRACK)
    assert pair.joiner.state.holders["deck:3"] == "inviter"
    assert same_booth(pair.inviter, pair.joiner)


def test_yield_grants_and_loads_at_once() -> None:
    pair = Pair()
    _keep_renewing(pair, "joiner", T0 + 200_000)
    pair.act("inviter", _load(4), T0 + 200_000)
    applied = pair.act("joiner", {"op": "yield", "unit": "deck:4"}, T0 + 300_000)
    assert _names(applied) == [("grab", "granted"), ("load", None)]
    assert _track_on(pair, 4) == (TRACK, TRACK)


def test_refuse_ends_it_and_nothing_loads_even_after_the_fallback() -> None:
    pair = Pair()
    _keep_renewing(pair, "joiner", T0 + 200_000)
    pair.act("inviter", _load(3), T0 + 200_000)
    assert _names(pair.act("joiner", {"op": "refuse", "unit": "deck:3"}, T0 + 500_000)) == [("refuse", None)]
    assert pair.tick(T0 + 200_000 + 5 * GRAB_FALLBACK_US) == []
    assert _track_on(pair, 3) == (None, None)
    assert pair.inviter.state.holders["deck:3"] == "joiner"


def test_a_lapsed_lease_is_granted_and_loaded_in_one_decision() -> None:
    pair = Pair()
    applied = pair.act("inviter", _load(3), T0 + LEASE_US + 1)
    assert _names(applied) == [("grab", "granted"), ("load", None)]
    assert applied[0]["op"]["reason"] == "lapsed"


def test_the_joiner_loads_the_inviters_deck_through_the_arbiter() -> None:
    pair = Pair()
    _keep_renewing(pair, "inviter", T0 + 100_000)
    assert _names(pair.act("joiner", _load(1), T0 + 100_000)) == [("grab", "ask")]
    granted = pair.tick(T0 + 100_000 + GRAB_FALLBACK_US)
    assert _names(granted) == [("grab", "granted"), ("load", None)]
    assert all(c["by"] == "joiner" for c in granted)
    assert _track_on(pair, 1) == (TRACK, TRACK)


def test_the_turn_lock_refuses_the_load_with_locked_and_commits_nothing() -> None:
    pair = Pair()
    pair.act("inviter", {"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0 + 1)
    seq = pair.inviter.state.seq
    assert pair.act("joiner", _load(1), T0 + 2) == []
    assert pair.inviter.state.seq == seq, "if a locked load commits anything then broken"
    refusals = [e for e in pair.events["joiner"] if e["type"] == "refusal"]
    assert refusals and refusals[-1]["reason"] == "locked"
    out = pair.inviter.intent_op({"op": "load", "deck": 1, "track": TRACK}, T0 + 3)
    assert out.refused is None, "control: the lock owner's own load passes"


def test_loading_a_deck_you_hold_commits_only_the_load() -> None:
    pair = Pair()
    assert _names(pair.act("inviter", _load(1), T0 + 1)) == [("load", None)]


def test_play_on_a_deck_you_do_not_hold_is_still_not_holder() -> None:
    pair = Pair()
    out = pair.inviter.intent_op({"op": "play", "deck": 3, "at_link_us": T0, "pos_frames": 0}, T0 + 1)
    assert out.refused == "not_holder", "if any deck op (not just load) grabs then broken"
    out = pair.joiner.intent_op({"op": "play", "deck": 1, "at_link_us": T0, "pos_frames": 0}, T0 + 1)
    assert out.refused == "not_holder"


def test_a_second_load_while_the_ask_waits_is_already_asked() -> None:
    pair = Pair()
    _keep_renewing(pair, "joiner", T0 + 100_000)
    pair.act("inviter", _load(3), T0 + 100_000)
    out = pair.inviter.intent_op(_load(3), T0 + 200_000)
    assert out.refused == "already_asked"
