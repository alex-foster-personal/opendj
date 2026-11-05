"""B2BBOOTH-06..11 on the ENGINE (S1b): grab lease, 1 s fallback, refuse, turn lock, settings.

Ported from the page's ``b2b-booth-lease.test.mjs`` (wave 1), every boundary pinned from BOTH sides
so an overshooting fix fails as loudly as the bug. The joiner's clock runs 37 ms off the
inviter's in every booth here, so the lease is judged on the inviter's clock after conversion.

- [if] a DJ grabs a unit whose lease lapsed [then] it is granted at once, reason lapsed; at exactly the lease end it is asked
- [if] the holder answers yield [then] the grab is granted, reason yield, on both laptops
- [if] the holder stays silent [then] the grab is granted at 1.0 s and 1.1 s, never at 0.9 s or 0.999999 s
- [if] the holder refuses within 1 s [then] the grab ends refused; a refuse at 1 s is too late
- [if] lock_half_on_turn is on for the ON AIR DJ [then] the partner's grab of that half, its strips, crossfader or master ends locked
- [if] the lock is off [then] the same grab is asked, never locked
- [if] the turn moves [then] the lock moves and the new ON AIR DJ holds their half
- [if] audio_laptop or two_outputs changes [then] role flips on both laptops through the log
- [if] the engine's lease or turn lock breaks any line above [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.b2b_booth.lease import GRAB_FALLBACK_US, LEASE_US, RENEW_INTERVAL_US
from tests.b2b_booth.booth_pair import T0, Pair, last_op, same_booth

pytestmark = [pytest.mark.requirement(f"B2BBOOTH-{n:02d}") for n in range(6, 12)]

SKEW = 37_000


def _booth() -> Pair:
    return Pair(offset_us=SKEW)


def _holder(pair: Pair, side: str, unit: str) -> str | None:
    return pair.core(side).state.holders[unit]


def test_constants_are_the_contracts() -> None:
    assert (RENEW_INTERVAL_US, LEASE_US, GRAB_FALLBACK_US) == (250_000, 750_000, 1_000_000)


@pytest.mark.parametrize(("delta", "expected"), [(0, "ask"), (1, "granted")])
def test_lapsed_lease_is_granted_one_microsecond_after_its_end_never_at_it(delta: int, expected: str) -> None:
    b = _booth()
    # deck:3 belongs to the joiner and was last leased at T0 (seed); it never renewed.
    applied = b.act("inviter", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + LEASE_US + delta)
    op = last_op(applied)
    assert op is not None and op["phase"] == expected
    if expected == "granted":
        assert op["reason"] == "lapsed"
        assert _holder(b, "inviter", "deck:3") == _holder(b, "joiner", "deck:3") == "inviter"


def test_renewals_keep_a_lease_alive_so_a_grab_is_asked() -> None:
    b = _booth()
    for t in range(T0, T0 + 3_000_001, RENEW_INTERVAL_US):
        b.renew("joiner", t)
    op = last_op(b.act("inviter", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + 3_000_000))
    assert op == {"op": "grab", "unit": "deck:3", "phase": "ask"}
    assert b.joiner.state.pending["deck:3"] == "inviter", "the holder sees the ask"


def test_renewal_uses_the_converted_clock() -> None:
    """Control for the 37 ms skew: renewing in the JOINER's raw clock would leave the lease 37 ms
    longer than it is; the arbiter sees exactly LEASE_US past the converted renew."""
    b = _booth()
    b.renew("joiner", T0 + 100_000)
    assert b.inviter.clock.lease_until("deck:3") == T0 + 100_000 + LEASE_US


def test_yield_grants_on_both_laptops() -> None:
    b = _booth()
    b.renew("joiner", T0 + 100_000)
    b.act("inviter", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + 200_000)
    op = last_op(b.act("joiner", {"op": "yield", "unit": "deck:3"}, T0 + 300_000))
    assert op == {"op": "grab", "unit": "deck:3", "phase": "granted", "reason": "yield"}
    assert _holder(b, "joiner", "deck:3") == "inviter"
    assert same_booth(b.inviter, b.joiner)


@pytest.mark.parametrize("after", [900_000, 950_000, 999_999])
def test_silence_is_not_enough_before_one_second(after: int) -> None:
    b = _booth()
    asked = T0 + 200_000
    b.act("joiner", {"op": "grab", "unit": "deck:1", "phase": "ask"}, asked)
    b.renew("inviter", asked + after)  # the holder is alive, just not answering
    assert b.tick(asked + after) == []
    assert _holder(b, "joiner", "deck:1") == "inviter"


@pytest.mark.parametrize("after", [1_000_000, 1_100_000])
def test_silence_grants_at_one_second_and_after(after: int) -> None:
    b = _booth()
    asked = T0 + 200_000
    b.act("joiner", {"op": "grab", "unit": "deck:1", "phase": "ask"}, asked)
    op = last_op(b.tick(asked + after))
    assert op == {"op": "grab", "unit": "deck:1", "phase": "granted", "reason": "silence"}
    assert _holder(b, "joiner", "deck:1") == _holder(b, "inviter", "deck:1") == "joiner"


def test_refuse_within_one_second_ends_refused_and_the_holder_keeps_it() -> None:
    b = _booth()
    asked = T0 + 100_000
    b.act("joiner", {"op": "grab", "unit": "strip:2", "phase": "ask"}, asked)
    applied = b.act("inviter", {"op": "refuse", "unit": "strip:2"}, asked + 999_999)
    assert last_op(applied) == {"op": "refuse", "unit": "strip:2"}
    assert applied[-1]["by"] == "joiner", "refuse names the grabber whose grab ended"
    assert _holder(b, "joiner", "strip:2") == "inviter"
    assert b.joiner.state.pending["strip:2"] is None
    assert b.tick(asked + 2_000_000) == [], "a refused grab is not granted later"


@pytest.mark.parametrize(("after", "refused"), [(999_999, None), (1_000_000, "too_late"), (1_100_000, "too_late")])
def test_a_refuse_at_one_second_or_later_is_too_late(after: int, refused: str | None) -> None:
    b = _booth()
    asked = T0 + 100_000
    b.act("joiner", {"op": "grab", "unit": "strip:2", "phase": "ask"}, asked)
    assert b.inviter.intent_op({"op": "refuse", "unit": "strip:2"}, asked + after).refused == refused


def test_release_leaves_a_unit_unheld_and_the_next_grab_is_granted_at_once() -> None:
    b = _booth()
    b.act("joiner", {"op": "release", "unit": "deck:4"}, T0 + 1)
    assert _holder(b, "inviter", "deck:4") is None
    assert last_op(b.act("inviter", {"op": "grab", "unit": "deck:4", "phase": "ask"}, T0 + 2)) == {
        "op": "grab", "unit": "deck:4", "phase": "granted", "reason": "unheld"}


def test_a_deck_op_from_a_side_that_does_not_hold_the_deck_is_never_committed() -> None:
    b = _booth()
    # The joiner's own engine refuses at the source (instant feedback) ...
    assert b.joiner.intent_op({"op": "play", "deck": 1, "at_link_us": 1, "pos_frames": 0}, T0).refused == "not_holder"
    # ... and the inviter refuses a proposal that got past it, answering with the reason.
    out = b.inviter.receive(
        {"v": 1, "epoch": b.joiner.epoch, "sent_us": T0, "messages": [
            {"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "play", "deck": 1, "at_link_us": T0 + SKEW, "pos_frames": 0}}]},
        T0)
    assert [e for e in out.events if e["type"] == "op"] == []
    assert out.answers == [{"pid": 1, "refused": "not_holder"}]
    assert b.inviter.counters.refused == 1


# ---------------------------------------------------------------- turn lock (B2BBOOTH-10)


@pytest.mark.parametrize("unit", ["deck:1", "deck:2", "strip:1", "strip:2", "xfader", "master"])
def test_lock_on_and_on_air_the_partners_grab_ends_locked(unit: str) -> None:
    b = _booth()
    b.act("inviter", {"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0 + 1)
    assert last_op(b.act("joiner", {"op": "grab", "unit": unit, "phase": "ask"}, T0 + 2)) == {"op": "locked", "unit": unit}
    assert _holder(b, "joiner", unit) == "inviter"


def test_lock_on_leaves_the_partners_own_half_alone() -> None:
    b = _booth()
    b.act("inviter", {"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0 + 1)
    assert b.act("joiner", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + 3) == [], "the joiner already holds deck:3"


def test_lock_off_the_same_grab_is_asked_then_granted_never_locked() -> None:
    b = _booth()
    assert last_op(b.act("joiner", {"op": "grab", "unit": "deck:1", "phase": "ask"}, T0 + 2)) == {
        "op": "grab", "unit": "deck:1", "phase": "ask"}
    op = last_op(b.tick(T0 + 2 + GRAB_FALLBACK_US))
    assert op is not None and op["reason"] == "silence"


def test_a_lock_set_by_the_dj_not_on_air_does_nothing() -> None:
    b = _booth()
    b.act("joiner", {"op": "setting", "side": "joiner", "lock_half_on_turn": True}, T0 + 1)
    op = last_op(b.act("inviter", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + LEASE_US + 10))
    assert op is not None and op["op"] == "grab"


def test_onair_moves_the_lock_and_hands_the_new_on_air_dj_their_half_back() -> None:
    b = _booth()
    b.act("joiner", {"op": "setting", "side": "joiner", "lock_half_on_turn": True}, T0 + 1)
    b.act("inviter", {"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0 + 2)
    b.act("inviter", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + 3)
    b.tick(T0 + 3 + GRAB_FALLBACK_US)
    assert _holder(b, "joiner", "deck:3") == "inviter"
    b.act("joiner", {"op": "onair", "side": "joiner"}, T0 + 2_000_000)
    for unit in ("deck:3", "deck:4", "strip:3", "strip:4", "xfader", "master"):
        assert _holder(b, "joiner", unit) == _holder(b, "inviter", unit) == "joiner", unit
    assert last_op(b.act("inviter", {"op": "grab", "unit": "deck:3", "phase": "ask"}, T0 + 2_000_001)) == {
        "op": "locked", "unit": "deck:3"}
    freed = last_op(b.act("joiner", {"op": "grab", "unit": "deck:1", "phase": "ask"}, T0 + 2_000_002))
    assert freed is not None and freed["op"] == "grab", "the inviter's half is no longer locked off the air"
    assert same_booth(b.inviter, b.joiner)


def test_the_lock_never_locks_out_its_own_dj() -> None:
    b = _booth()
    b.act("inviter", {"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0 + 1)
    b.act("inviter", {"op": "release", "unit": "deck:2"}, T0 + 2)
    assert _holder(b, "joiner", "deck:2") is None
    assert last_op(b.act("joiner", {"op": "grab", "unit": "deck:2", "phase": "ask"}, T0 + 3)) == {"op": "locked", "unit": "deck:2"}
    assert last_op(b.act("inviter", {"op": "grab", "unit": "deck:2", "phase": "ask"}, T0 + 4)) == {
        "op": "grab", "unit": "deck:2", "phase": "granted", "reason": "unheld"}


def test_a_lock_that_comes_on_while_a_grab_waits_ends_it_locked() -> None:
    b = _booth()
    b.renew("inviter", T0 + 1)
    b.act("joiner", {"op": "grab", "unit": "deck:1", "phase": "ask"}, T0 + 2)
    b.act("inviter", {"op": "setting", "side": "inviter", "lock_half_on_turn": True}, T0 + 3)
    assert last_op(b.tick(T0 + 4)) == {"op": "locked", "unit": "deck:1"}
    assert b.tick(T0 + 2 + GRAB_FALLBACK_US) == [], "the ended ask is never granted later"


def test_a_dj_cannot_set_the_partners_lock() -> None:
    b = _booth()
    applied = b.act("joiner", {"op": "setting", "side": "inviter", "lock_half_on_turn": False}, T0)
    assert applied == []
    refusals = [e for e in b.events["joiner"] if e["type"] == "refusal"]
    assert refusals and refusals[-1]["reason"] == "not_your_setting", "the engine's reason reaches the proposer's page"


# ---------------------------------------------------------------- settings (B2BBOOTH-11)


def test_audio_laptop_defaults_to_the_inviter_and_flips_role_on_both() -> None:
    b = _booth()
    assert (b.inviter.role, b.joiner.role) == ("audio", "control")
    applied = b.act("joiner", {"op": "setting", "side": "joiner", "audio_laptop": "joiner"}, T0 + 1)
    assert len(applied) == 1
    assert (b.inviter.role, b.joiner.role) == ("control", "audio")
    settings = [e for e in b.events["joiner"] if e["type"] == "settings"]
    assert settings[-1]["role"] == "audio" and settings[-1]["settings"]["audio_laptop"] == "joiner"


def test_two_outputs_makes_both_audio_and_off_restores_the_rule() -> None:
    b = _booth()
    b.act("inviter", {"op": "setting", "side": "inviter", "two_outputs": True}, T0 + 1)
    assert (b.inviter.role, b.joiner.role) == ("audio", "audio")
    b.act("joiner", {"op": "setting", "side": "joiner", "two_outputs": False}, T0 + 2)
    assert (b.inviter.role, b.joiner.role) == ("audio", "control")
