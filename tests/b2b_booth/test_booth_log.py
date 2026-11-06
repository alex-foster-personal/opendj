"""B2BBOOTH-03..05 and B2BBOOTH-92 on the ENGINE (S1b): one seq order, stale epochs, gaps, controls,
the partner clock. Ported from the page's ``b2b-booth-log.test.mjs`` (wave 1).

- [if] both DJs propose interleaved [then] both engines apply one identical seq order 1..N with no hole
- [if] a message carries another epoch [then] it is dropped, counted stale_epoch, never applied
- [if] commits 1, 3, 4 arrive [then] only 1 applies, ONE resend from 2 is asked, the resend fills 2, 3, 4 in order
- [if] an older cseq or a non-holder's control arrives [then] it is dropped and counted
- [if] a partner time reaches the page [then] it is on THIS laptop's clock (least-RTT round trip), never raw
- [if] the inviter re-opens [then] the joiner moves to the new epoch only through a snapshot it asked for
- [if] the engine's log or clock breaks any line above [then] fail, [else stop].
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from apps.b2b_booth.core import BoothCore, Out
from apps.b2b_booth.lease import BoothState
from apps.b2b_booth.log import PeerClock
from tests.b2b_booth.booth_pair import E1, E2, T0, Pair, committed_ops, same_booth, wire

pytestmark = [pytest.mark.requirement(r) for r in ("B2BBOOTH-03", "B2BBOOTH-04", "B2BBOOTH-05", "B2BBOOTH-92")]


def _play(deck: int, at: int) -> dict[str, Any]:
    return {"op": "play", "deck": deck, "at_link_us": at, "pos_frames": at % 1000}


@pytest.mark.parametrize("seed", [1, 2, 3, 42, 1234])
def test_interleaved_proposals_apply_identically(seed: int) -> None:
    rng = random.Random(seed)
    b = Pair()
    to_inviter: list[dict[str, Any]] = []
    to_joiner: list[dict[str, Any]] = []
    applied: dict[str, list[dict[str, Any]]] = {"inviter": [], "joiner": []}

    def route(side: str, out: Out, queue: list[dict[str, Any]]) -> None:
        queue += [wire("ops", [m]) for m in out.ops]
        applied[side] += committed_ops(out)

    now, proposed = T0, 0
    while proposed < 400 or to_inviter or to_joiner:
        now += 1000
        r = rng.random()
        if proposed < 400 and r < 0.25:
            proposed += 1
            route("inviter", b.inviter.intent_op(_play(1 + rng.randrange(2), now), now), to_joiner)
        elif proposed < 400 and r < 0.5:
            proposed += 1
            setting = {"op": "setting", "side": "joiner", "lock_half_on_turn": rng.random() < 0.5}
            op = setting if rng.random() < 0.1 else _play(3 + rng.randrange(2), now)
            route("joiner", b.joiner.intent_op(op, now), to_inviter)
        elif r < 0.75 and to_inviter:
            route("inviter", b.inviter.receive(to_inviter.pop(0), now), to_joiner)
        elif to_joiner:
            route("joiner", b.joiner.receive(to_joiner.pop(0), now), to_inviter)
    assert [c["seq"] for c in applied["inviter"]] == list(range(1, 401))
    assert applied["joiner"] == applied["inviter"], "the joiner applied the same ops in the same order"
    assert same_booth(b.inviter, b.joiner)
    assert {c["by"] for c in applied["inviter"]} == {"inviter", "joiner"}, "positive control: both sides interleaved"


def test_a_commit_of_another_epoch_is_dropped_and_counted_and_the_current_one_applies() -> None:
    b = Pair()
    good = b.inviter.intent_op(_play(1, 5), 5).ops[0]
    dropped = b.joiner.receive(wire("ops", [{**good, "epoch": E2}]), 6)
    assert committed_ops(dropped) == []
    assert b.joiner.applier.counters.stale_epoch == 1
    assert b.joiner.state.seq == 0
    applied = b.joiner.receive(wire("ops", [good]), 7)
    assert [c["seq"] for c in committed_ops(applied)] == [1], "negative control: the current epoch applies"
    assert b.joiner.applier.counters.stale_epoch == 1


def test_a_proposal_of_an_old_epoch_is_dropped_counted_and_answered_never_committed() -> None:
    b = Pair()
    proposal = {**b.joiner.intent_op(_play(3, 5), 5).ops[0], "epoch": E2}
    out = b.inviter.receive(wire("ops", [proposal]), 6)
    assert out.ops == []
    assert out.answers == [{"pid": 1, "refused": "stale_epoch"}]
    assert b.inviter.applier.counters.stale_epoch == 1
    assert b.inviter.sequencer is not None and b.inviter.sequencer.seq == 0


def test_stale_controls_renewals_and_positions_are_dropped_and_counted() -> None:
    b = Pair()
    ctl = b.inviter.intent_control("strip:1", "fader", 0.5, 10).ctl[0][1]
    pos = b.inviter.intent_position({"deck": 1, "pos_frames": 100.0, "at_link_us": 12, "rate": 1.0}).ctl[0][1]
    before = dict(b.joiner.controls)
    b.joiner.receive(wire("ctl", [{**ctl, "epoch": E2}]), 11)
    b.joiner.receive(wire("ctl", [{**pos, "epoch": E2}]), 12)
    assert b.joiner.applier.counters.stale_epoch == 2
    assert b.joiner.controls == before
    assert b.joiner.positions == {}


def test_a_joiner_with_no_epoch_adopts_only_a_snapshot() -> None:
    inviter = BoothCore("inviter", E1, 0)
    joiner = BoothCore("joiner", None, 0)
    commit = inviter.intent_op(_play(1, 5), 5).ops[0]
    joiner.receive(wire("ops", [commit]), 6)
    assert joiner.applier.counters.stale_epoch == 1, "a commit before the snapshot is not applied"
    joiner.receive(wire("ops", [inviter.snapshot_message()]), 7)
    assert joiner.epoch == E1
    assert joiner.state.seq == 1, "the snapshot carried the op"


def test_after_a_reopen_the_joiner_moves_epoch_only_through_the_snapshot_it_asked_for() -> None:
    b = Pair()
    b.act("inviter", _play(1, 1), T0 + 1)
    reopened = BoothCore("inviter", E2, T0 + 2)
    pushed = reopened.snapshot_message()
    b.joiner.receive(wire("ops", [pushed]), T0 + 3)
    assert b.joiner.epoch == E1, "a pushed snapshot of another epoch is stale, not adopted"
    assert b.joiner.applier.counters.stale_epoch == 1
    answer = reopened.receive(wire("ops", [b.joiner.hello()]), T0 + 4)
    assert [m["kind"] for m in answer.replies] == ["snapshot"]
    b.joiner.receive(wire("ops", answer.replies), T0 + 5, reply=True)
    assert b.joiner.epoch == E2 and b.joiner.state.seq == 0
    assert b.joiner.history == [], "the old epoch's ops are gone"


def test_a_gap_applies_nothing_out_of_order_asks_once_and_the_resend_heals_it() -> None:
    b = Pair()
    commits = [b.inviter.intent_op(_play(1, i), i).ops[0] for i in range(1, 5)]
    applied: list[int] = []
    asks: list[dict[str, Any]] = []
    for c in (commits[0], commits[2], commits[3]):
        out = b.joiner.receive(wire("ops", [c]), 10)
        applied += [x["seq"] for x in committed_ops(out)]
        asks += out.ops
    assert applied == [1]
    assert [(m["kind"], m["from"]) for m in asks] == [("resend", 2)], "one gap asks once"
    assert b.joiner.applier.counters.gaps == 1
    reask = b.joiner.reask_gap()
    assert reask is not None and reask["from"] == 2, "the gap is still open for a timed re-ask"
    answer = b.inviter.receive(wire("ops", asks), 11)
    assert [c["seq"] for c in answer.replies[0]["ops"]] == [2, 3, 4]
    applied += [x["seq"] for x in committed_ops(b.joiner.receive(wire("ops", answer.replies), 12, reply=True))]
    assert applied == [1, 2, 3, 4]
    assert b.joiner.applier.counters.duplicates == 2, "3 and 4 came twice and applied once"
    assert b.joiner.reask_gap() is None
    assert same_booth(b.inviter, b.joiner)


def test_the_reducer_refuses_an_out_of_order_apply_loudly() -> None:
    with pytest.raises(ValueError, match="in order only"):
        BoothState().apply({"seq": 2, "by": "inviter", "pid": None, "op": {"op": "end"}})


def test_random_drops_on_the_way_to_the_joiner_always_heal() -> None:
    rng = random.Random(7)
    b = Pair()
    applied: list[int] = []
    for i in range(1, 301):
        message = b.inviter.intent_op(_play(1, i), i).ops[0]
        if rng.random() < 0.1:
            continue  # lost
        out = b.joiner.receive(wire("ops", [message]), i)
        applied += [x["seq"] for x in committed_ops(out)]
        for ask in out.ops:
            answer = b.inviter.receive(wire("ops", [ask]), i)
            applied += [x["seq"] for x in committed_ops(b.joiner.receive(wire("ops", answer.replies), i, reply=True))]
    tail = b.joiner.reask_gap()
    if tail is not None:
        answer = b.inviter.receive(wire("ops", [tail]), 301)
        applied += [x["seq"] for x in committed_ops(b.joiner.receive(wire("ops", answer.replies), 301, reply=True))]
    assert applied == list(range(1, len(applied) + 1))
    assert b.joiner.applier.counters.gaps > 0, "positive control: drops really opened gaps"
    assert b.joiner.state.seq == b.inviter.state.seq == 300


def test_a_replaced_joiner_proposes_again_after_adopting_the_inviters_snapshot() -> None:
    b = Pair()
    b.act("joiner", _play(3, T0 + 1), T0 + 1)
    assert b.inviter._last_peer_pid == 1
    dup_before = b.inviter.counters.proposals_dup
    b.joiner = BoothCore("joiner", None, T0 + 2 + b.offset_us)
    b.joiner.peer_clock.add(T0 + 2 + b.offset_us, T0 + 2, T0 + 2 + b.offset_us)
    b.joiner.receive(wire("ops", [b.inviter.snapshot_message()]), T0 + 3, reply=True)
    assert b.joiner.epoch == E1
    assert b.joiner.state.seq == b.inviter.state.seq == 1
    applied = b.act("joiner", _play(3, T0 + 2), T0 + 2)
    assert len(applied) == 1
    assert applied[0]["op"]["op"] == "play"
    assert applied[0]["seq"] == 2
    assert b.inviter.counters.proposals_dup == dup_before
    assert same_booth(b.inviter, b.joiner)


def test_a_replaced_joiner_hello_does_not_reuse_the_old_sessions_refusals() -> None:
    b = Pair()
    proposal = wire(
        "ops",
        b.joiner.intent_op({"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0).ops,
    )
    first = b.inviter.receive(proposal, T0)
    assert first.answers == [{"pid": 1, "refused": "not_your_setting"}]
    b.replace_joiner(T0 + 1, hello=True)
    applied = b.act("joiner", _play(3, T0 + 2), T0 + 2)
    assert len(applied) == 1
    assert applied[0]["op"]["op"] == "play"
    assert same_booth(b.inviter, b.joiner)


def test_a_retried_proposal_is_committed_once_and_answered_the_same() -> None:
    b = Pair()
    proposal = wire("ops", b.joiner.intent_op({"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0).ops)
    first = b.inviter.receive(proposal, T0)
    again = b.inviter.receive(proposal, T0 + 1)
    assert first.answers == again.answers == [{"pid": 1, "refused": "not_your_setting"}]
    play = wire("ops", b.joiner.intent_op(_play(3, 2), T0 + 2).ops)
    assert len(committed_ops(b.inviter.receive(play, T0 + 2))) == 1
    assert committed_ops(b.inviter.receive(play, T0 + 3)) == [], "a retried POST never commits twice"
    assert b.inviter.counters.proposals_dup == 2


# ---------------------------------------------------------------- controls


def test_controls_keep_the_newest_from_the_holder() -> None:
    b = Pair()
    m1 = b.joiner.intent_control("strip:3", "eq.low", 0.2, 10).ctl[0][1]
    m2 = b.joiner.intent_control("strip:3", "eq.low", 0.4, 11).ctl[0][1]
    b.inviter.receive(wire("ctl", [m2]), 12)
    b.inviter.receive(wire("ctl", [m1]), 13)
    assert b.inviter.controls["strip:3|eq.low"]["value"] == 0.4
    assert b.inviter.counters.ctl_stale == 1
    m3 = b.joiner.intent_control("strip:3", "eq.low", 0.6, 14).ctl[0][1]
    b.inviter.receive(wire("ctl", [m3]), 15)
    assert b.inviter.controls["strip:3|eq.low"]["value"] == 0.6, "control for the overshoot: a newer cseq from the same side wins"
    assert b.inviter.counters.ctl_stale == 1
    b.inviter.receive(wire("ctl", [{**m2, "unit": "strip:1", "cseq": 99}]), 14)
    assert b.inviter.counters.ctl_not_holder == 1
    assert b.joiner.intent_control("strip:1", "fader", 1.0, 15).refused == "not_holder"


def test_trim_travels_and_a_local_only_output_never_does() -> None:
    from apps.b2b_booth.protocol import BoothError, validate_ctl_name

    b = Pair()
    out = b.control("joiner", "strip:3", "trim", 0.8, T0)
    assert out.refused is None
    got = [e["control"] for e in b.events["inviter"] if e["type"] == "control"]
    assert got[-1]["ctl"] == "trim" and got[-1]["value"] == 0.8
    for local in ("cue_volume", "cue_mix", "cue_bus", "master_out_level"):
        with pytest.raises(BoothError) as err:
            validate_ctl_name(local)
        assert err.value.code == "local_only"


# ---------------------------------------------------------------- the partner clock


def test_the_least_round_trip_wins_and_conversion_never_guesses() -> None:
    clock = PeerClock()
    with pytest.raises(ValueError, match="no round trip"):
        clock.to_local(5)
    clock.add(1_000, 51_000 + 40_000, 81_000)  # 80 ms trip, asymmetric
    clock.add(2_000, 53_000, 4_000)  # 2 ms trip: partner 50 ms ahead at the midpoint
    assert clock.rtt_us == 2_000
    assert clock.offset_us == 50_000
    clock.add(3_000, 3_000 + 50_000 + 300_000, 63_000)  # a later, slower trip: it must not move the estimate
    assert (clock.rtt_us, clock.offset_us) == (2_000, 50_000), "the least round trip wins, not the newest"
    assert clock.to_local(150_000) == 100_000
    with pytest.raises(ValueError, match="negative"):
        clock.add(10, 0, 5)


@pytest.mark.parametrize("skew", [-37_000, 0, 250_000])
def test_partner_times_reach_the_page_on_this_laptops_clock(skew: int) -> None:
    b = Pair(offset_us=skew)
    b.control("joiner", "strip:3", "fader", 0.5, T0 + 1_000)
    got = [e for e in b.events["inviter"] if e["type"] == "control"][-1]
    assert got["control"]["at_link_us"] == T0 + 1_000, "the joiner's stamp, converted to the inviter's clock"
    b.act("inviter", _play(1, T0 + 2_000), T0 + 2_000)
    op = [e for e in b.events["joiner"] if e["type"] == "op"][-1]["committed"]["op"]
    assert op["at_link_us"] == T0 + 2_000 + skew, "the joiner's page gets the inviter's time on its own clock"
    assert b.joiner.history[-1]["op"]["at_link_us"] == T0 + 2_000, "the log itself stays on the inviter's clock"
    b.act("joiner", _play(3, T0 + 3_000 + skew), T0 + 3_000)
    assert b.inviter.history[-1]["op"]["at_link_us"] == T0 + 3_000, "a joiner proposal is committed on the inviter's clock"


def test_a_proposal_whose_time_converts_below_zero_is_refused_never_committed() -> None:
    """Control for the inviter's conversion: a joiner time that lands before the inviter's clock zero
    would put an invalid op in the log (and stall the ordered lane at the joiner's validator)."""
    b = Pair(offset_us=5_000)
    applied = b.act("joiner", _play(3, 1_000), T0)  # 1 ms on the joiner's clock is -4 ms on the inviter's
    assert applied == []
    assert [e["reason"] for e in b.events["joiner"] if e["type"] == "refusal"] == ["bad_time"]
    ok = b.act("joiner", _play(3, 6_000), T0)
    assert [c["op"]["at_link_us"] for c in ok] == [1_000], "negative control: a time that converts to >= 0 commits"
