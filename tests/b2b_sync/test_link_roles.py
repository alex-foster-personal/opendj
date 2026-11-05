"""LINKROLE-01/02: the engine's take-over authority and the one peer-liveness verdict.

Acceptance (each maps to an assertion below):
- [if] both claim ON AIR [then] the higher take-over number keeps it, the lower yields [else two ON AIR]
- [if] the numbers tie and both ids are known [then] the lower id keeps it, on both sides
- [if] the numbers tie and an id is unknown [then] this side yields and says why [else a throw that left both ON AIR]
- [if] a claim names this engine's own id [then] it is not a clash (a peer URL pointing back at itself)
- [if] a take-over is pressed after seeing the peer's claim [then] it outranks that claim
- [if] the peer's claim is old (stale) but outranks this side [then] this side still yields [else one-sided staleness keeps both ON AIR]
- [if] a peer answer is older than the one limit [then] it is not live, with its age [else two limits disagree]
- [if] the peer holds the turn and is live [then] a FOLLOWING engine refuses to write the session tempo
- [if] the claim travels as a query parameter [then] it round-trips, and a malformed one is refused (None), never a default
"""

from __future__ import annotations

import pytest

from apps.b2b_sync import link_roles as lr

# ----------------------------------------------------------------------------- the clash rule


def _claim(role: lr.LinkRole, seq: int | None, peer_id: str | None) -> lr.RoleClaim:
    return lr.RoleClaim(role=role, took_seq=seq, peer_id=peer_id)


def test_higher_take_over_number_keeps_on_both_sides() -> None:
    a, b = _claim("on_air", 3, "aaa"), _claim("on_air", 4, "bbb")
    assert lr.resolve_clash(a, b) == "yield"
    assert lr.resolve_clash(b, a) == "keep"


def test_tie_with_both_ids_goes_to_the_lower_id_on_both_sides() -> None:
    a, b = _claim("on_air", 2, "aaa"), _claim("on_air", 2, "bbb")
    assert lr.resolve_clash(a, b) == "keep"
    assert lr.resolve_clash(b, a) == "yield"


def test_tie_with_an_unknown_id_yields_and_never_throws() -> None:
    assert lr.resolve_clash(_claim("on_air", 2, "aaa"), _claim("on_air", 2, None)) == "yield"
    assert lr.resolve_clash(_claim("on_air", 2, None), _claim("on_air", 2, "bbb")) == "yield"


def test_an_unnumbered_on_air_claim_ranks_below_every_press() -> None:
    assert lr.resolve_clash(_claim("on_air", 1, "zzz"), _claim("on_air", None, "aaa")) == "keep"
    assert lr.resolve_clash(_claim("on_air", None, "aaa"), _claim("on_air", 1, "zzz")) == "yield"


def test_a_claim_with_this_engines_own_id_is_not_a_clash() -> None:
    assert lr.resolve_clash(_claim("on_air", 1, "same"), _claim("on_air", 9, "same")) == "keep"


# ----------------------------------------------------------------------------- the authority


def test_take_over_outranks_the_peer_claim_it_has_seen() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.observe_peer(_claim("on_air", 5, "bbb"), "silver")
    mine = auth.take_over()
    assert mine == _claim("on_air", 6, "aaa")
    assert auth.observe_peer(_claim("on_air", 5, "bbb"), "silver") is None, "the newer press keeps ON AIR"
    assert auth.role == "on_air"


def test_a_later_peer_take_over_makes_this_engine_follow_with_the_reason() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.take_over()
    reason = auth.observe_peer(_claim("on_air", 2, "bbb"), "silver")
    assert auth.role == "following" and auth.took_seq is None
    assert reason is not None and "Silver took over just after you" in reason
    assert auth.snapshot()["reason"] == reason


def test_a_tie_without_ids_yields_and_says_so() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.take_over()
    reason = auth.observe_peer(_claim("on_air", 1, None), "silver")
    assert auth.role == "following"
    assert reason is not None and "neither leads" in reason


def test_a_stale_claim_that_outranks_still_wins() -> None:
    """The verdict is a function of the two claims only, so two engines that saw the same two
    claims agree; liveness never decides who yields (it decided it in the page, per side)."""
    auth = lr.RoleAuthority("aaa")
    auth.take_over()
    auth.observe_peer(_claim("on_air", 2, "bbb"), "silver")
    assert auth.role == "following"


def test_following_peer_claims_raise_the_seen_number_and_never_yield() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.take_over()
    assert auth.observe_peer(_claim("following", None, "bbb"), "silver") is None
    assert auth.role == "on_air"
    auth.observe_peer(_claim("on_air", 7, "bbb"), "silver")
    assert auth.take_over().took_seq == 8


def test_follow_clears_the_number_and_keeps_a_reason() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.take_over()
    auth.follow("Session timeline jumped 1.00 beats (another peer moved it): now FOLLOWING")
    assert auth.claim() == _claim("following", None, "aaa")
    assert auth.snapshot()["reason"].startswith("Session timeline jumped")


def test_the_peer_holds_the_turn_only_while_live_and_on_air() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.observe_peer(_claim("on_air", 1, "bbb"), "silver")
    assert auth.peer_holds_turn(now_s=10.0) is False, "never answered: not live, cannot hold the tempo"
    auth.note_peer_answer(mirror_age_ms=500.0, received_at_s=10.0, peer_name="silver")
    assert auth.peer_holds_turn(now_s=11.0) is True
    assert auth.peer_holds_turn(now_s=10.0 + 2.6) is False, (
        "500 ms + 2.6 s is past the limit: a dead peer cannot hold it"
    )
    auth.take_over()
    assert auth.peer_holds_turn(now_s=11.0) is False, "control: this engine holds it now"


def test_liveness_ages_from_the_engine_answer_and_reads_never_answered_first() -> None:
    auth = lr.RoleAuthority("aaa")
    assert auth.liveness(now_s=5.0) == lr.NEVER_ANSWERED
    auth.note_peer_answer(mirror_age_ms=250.0, received_at_s=5.0, peer_name=None)
    assert auth.liveness(now_s=6.0) == lr.PeerLiveness(live=True, age_ms=1250.0, max_age_ms=lr.PEER_LIVE_MAX_MS)
    assert auth.snapshot_with_liveness(now_s=6.0)["peer_liveness"]["age_ms"] == 1250.0


def test_an_engine_id_is_required() -> None:
    with pytest.raises(ValueError):
        lr.RoleAuthority("")


# ----------------------------------------------------------------------------- liveness


def test_liveness_one_limit_with_its_age() -> None:
    fresh = lr.peer_liveness(mirror_age_ms=400.0, since_answer_ms=100.0)
    assert fresh == lr.PeerLiveness(live=True, age_ms=500.0, max_age_ms=lr.PEER_LIVE_MAX_MS)
    edge = lr.peer_liveness(mirror_age_ms=lr.PEER_LIVE_MAX_MS, since_answer_ms=0.0)
    assert edge.live is True
    old = lr.peer_liveness(mirror_age_ms=lr.PEER_LIVE_MAX_MS, since_answer_ms=1.0)
    assert old.live is False and old.age_ms == lr.PEER_LIVE_MAX_MS + 1.0


def test_liveness_refuses_negative_or_non_finite_ages() -> None:
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            lr.peer_liveness(mirror_age_ms=bad, since_answer_ms=0.0)


def test_never_answered_is_not_live_and_has_no_age() -> None:
    expected = lr.PeerLiveness(live=False, age_ms=None, max_age_ms=lr.PEER_LIVE_MAX_MS)
    assert expected == lr.NEVER_ANSWERED


# ----------------------------------------------------------------------------- wire forms


@pytest.mark.parametrize(
    "claim",
    [_claim("on_air", 12, "0a1b2c"), _claim("following", None, "0a1b2c"), _claim("on_air", None, "0a1b2c")],
)
def test_claim_round_trips_through_the_query_parameter(claim: lr.RoleClaim) -> None:
    assert lr.parse_claim_param(lr.claim_param(claim)) == claim


@pytest.mark.parametrize(
    "raw", ["", "boss.1.abc", "on_air.-1.abc", "on_air.1.", "on_air.1.a b", "on_air.x.abc", "on_air.1"]
)
def test_a_malformed_claim_parameter_is_refused(raw: str) -> None:
    assert lr.parse_claim_param(raw) is None


def test_claim_from_the_now_playing_answer() -> None:
    assert lr.parse_claim({"role": "on_air", "took_seq": 3, "peer_id": "bbb"}) == _claim("on_air", 3, "bbb")
    assert lr.parse_claim({"role": "on_air", "took_seq": True, "peer_id": "bbb"}) is None
    assert lr.parse_claim({"role": "boss"}) is None
    assert lr.parse_claim(None) is None


def test_forgetting_the_peer_link_keeps_the_seen_number() -> None:
    auth = lr.RoleAuthority("aaa")
    auth.note_peer_answer(mirror_age_ms=0.0, received_at_s=1.0, peer_name="silver")
    auth.observe_peer(_claim("on_air", 4, "bbb"))
    auth.peer_takes_claims = True
    auth.forget_peer_link()
    assert auth.liveness(now_s=1.0) == lr.NEVER_ANSWERED
    assert auth.peer_claim is None and auth.peer_takes_claims is None
    assert auth.take_over().took_seq == 5


# ----------------------------------------------------------------------------- timeline-jump demotion


def test_a_timeline_jump_is_not_a_take_over_while_the_live_partner_follows() -> None:
    """LINKROLE-03: the page saw the session timeline jump while ON AIR. The engine decides: a live
    Open DJ partner that claims FOLLOWING did not take over, so this side keeps ON AIR."""
    auth = lr.RoleAuthority("aaa")
    auth.take_over()
    auth.note_peer_answer(mirror_age_ms=100.0, received_at_s=10.0, peer_name="silver")
    auth.observe_peer(_claim("following", None, "bbb"), "silver")
    kept = auth.timeline_jumped(reason="Session timeline jumped 1.00 beats", now_s=10.5)
    assert kept is False and auth.role == "on_air", "a jump the following partner did not cause must not demote"
    assert auth.snapshot()["reason"] is None


def test_a_timeline_jump_demotes_when_no_live_partner_claims_following() -> None:
    reason = "Session timeline jumped 1.00 beats (another peer moved it): now FOLLOWING"
    commercial = lr.RoleAuthority("aaa")
    commercial.take_over()
    assert commercial.timeline_jumped(reason=reason, now_s=1.0) is True, "no Open DJ partner (a commercial peer): the jump demotes, as before"
    assert commercial.role == "following" and commercial.snapshot()["reason"] == reason
    stale = lr.RoleAuthority("aaa")
    stale.take_over()
    stale.note_peer_answer(mirror_age_ms=100.0, received_at_s=1.0, peer_name="silver")
    stale.observe_peer(_claim("following", None, "bbb"), "silver")
    assert stale.timeline_jumped(reason=reason, now_s=20.0) is True, "a partner gone quiet proves nothing: demote"
    on_air = lr.RoleAuthority("aaa")
    on_air.take_over()
    on_air.note_peer_answer(mirror_age_ms=100.0, received_at_s=1.0, peer_name="silver")
    on_air.observe_peer(_claim("on_air", None, "bbb"), "silver")
    assert on_air.timeline_jumped(reason=reason, now_s=1.5) is True, "control: a partner claiming ON AIR may have moved it"


def test_a_timeline_jump_while_following_changes_nothing() -> None:
    auth = lr.RoleAuthority("aaa")
    assert auth.timeline_jumped(reason="jumped", now_s=1.0) is False
    assert auth.role == "following" and auth.epoch == 0


# ------------------------------------------------------------------ LINKLANE-01: the lane grid lead


def _live(auth: lr.RoleAuthority, peer: lr.RoleClaim) -> lr.RoleAuthority:
    auth.observe_peer(peer, "silver")
    auth.note_peer_answer(mirror_age_ms=100.0, received_at_s=10.0, peer_name="silver")
    return auth


@pytest.mark.requirement("LINKLANE-01")
def test_the_on_air_side_leads_the_lane_grid_on_both_engines() -> None:
    """[if] ON AIR does not lead the lane grid on both engines [then] fail, [else stop]."""
    maintainer = lr.RoleAuthority("aaa")
    maintainer.take_over()
    peter = _live(lr.RoleAuthority("zzz"), maintainer.claim())
    _live(maintainer, peter.claim())
    assert maintainer.lane_lead(now_s=10.5)[0] == "self"
    assert peter.lane_lead(now_s=10.5)[0] == "peer", "ON AIR leads even with the higher id (control)"


@pytest.mark.requirement("LINKLANE-01")
def test_with_both_following_the_lower_engine_id_leads_on_both() -> None:
    """[if] two followers disagree on lane lead [then] fail, [else stop]."""
    low, high = lr.RoleAuthority("aaa"), lr.RoleAuthority("bbb")
    _live(low, high.claim())
    _live(high, low.claim())
    assert (low.lane_lead(now_s=10.5)[0], high.lane_lead(now_s=10.5)[0]) == ("self", "peer")


@pytest.mark.requirement("LINKLANE-01")
def test_with_no_live_partner_this_laptop_draws_its_own_grid() -> None:
    """[if] a missing partner does not fall back to self lane lead [then] fail, [else stop]."""
    auth = lr.RoleAuthority("bbb")
    lead, reason = auth.lane_lead(now_s=10.0)
    assert lead == "self" and "no live partner" in reason
    auth.observe_peer(_claim("following", None, "aaa"), "silver")
    assert auth.lane_lead(now_s=10.0)[0] == "self", "a claim never answered is not live"
    auth.note_peer_answer(mirror_age_ms=100.0, received_at_s=10.0, peer_name="silver")
    assert auth.lane_lead(now_s=10.5)[0] == "peer", "control: live, and the lower id leads"
    assert auth.lane_lead(now_s=14.0)[0] == "self", "past the liveness limit it is gone again"


@pytest.mark.requirement("LINKLANE-01")
def test_a_partner_with_no_engine_id_is_named_as_undecidable() -> None:
    """[if] a peer without an engine id is not named undecidable [then] fail, [else stop]."""
    auth = _live(lr.RoleAuthority("bbb"), _claim("following", None, None))
    lead, reason = auth.lane_lead(now_s=10.5)
    assert lead == "self" and "engine id" in reason
