"""The transition-in cue (AGENT-51): the store and the planner's in-window preference.

[if] a track carries a transition-in cue [then] the planner's in window starts on its bar, [else stop].

Regression one-liners:
  - if save_transition_in keeps two transition-in cues on one track then broken
  - if saving or clearing the transition-in cue touches a hot cue then broken
  - if a track with a transition-in cue gets any other in window first then broken
  - if a track WITHOUT one loses the rule-based intro window (the overshoot) then broken
  - if a cue off the downbeat is not snapped to the nearest bar then broken
  - if a cue mid-phrase is moved to the phrase start then broken
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.autodj.profile import ProfileError, profile_from_payloads, snap_to_downbeat
from apps.autodj.windows import USER_CUE_SCORE, mix_windows
from apps.shared.state import cue_store
from tests.autodj.fixtures import anlz_payload, profile, track_row

pytestmark = pytest.mark.requirement("AGENT-51")

SID = "trk-cue"
BAR_S = 240.0 / 128.0  # one bar at the fixtures' 128 BPM


def _state(cues: list[dict[str, Any]]) -> cue_store.StoredCues:
    return cue_store.StoredCues(
        stable_id=SID, cues=[cue_store.normalize_cue(c) for c in cues], generations={}, reversals={}, source="webui"
    )


def _saved(state: cue_store.StoredCues, in_ms: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blobs: list[dict[str, Any]] = []
    cue = cue_store.save_transition_in(state, in_ms, duration_ms=240_000, write=blobs.append)
    assert len(blobs) == 1
    return cue, blobs[0]["cues"]


def _profile_with_cue(in_ms: int) -> Any:
    anlz = anlz_payload(SID)
    anlz["cues"] = [{"kind": "transition_in", "slot": None, "in_ms": in_ms}]
    return profile_from_payloads(anlz, track_row(SID))


# ----- store -----------------------------------------------------------------


def test_save_sets_one_cue_and_moving_it_replaces_it() -> None:
    hot = {"kind": "hot_cue", "slot": "A", "in_ms": 1000}
    cue, cues = _saved(_state([hot]), 30_000)
    assert cue == {**cue, "kind": "transition_in", "slot": None, "in_ms": 30_000}
    moved, cues = _saved(_state(cues), 45_000)
    assert [c["in_ms"] for c in cues if c["kind"] == "transition_in"] == [45_000]
    assert [c for c in cues if c["kind"] == "hot_cue"] == [cue_store.normalize_cue(hot)]
    assert moved["in_ms"] == 45_000


def test_clear_removes_only_the_transition_in_cue_and_reports_a_no_op() -> None:
    hot = {"kind": "hot_cue", "slot": "B", "in_ms": 2000}
    _cue, cues = _saved(_state([hot]), 30_000)
    blobs: list[dict[str, Any]] = []
    assert cue_store.clear_transition_in(_state(cues), write=blobs.append) is True
    assert blobs[0]["cues"] == [cue_store.normalize_cue(hot)]
    assert cue_store.clear_transition_in(_state([hot]), write=blobs.append) is False
    assert len(blobs) == 1, "a clear with nothing to clear wrote a blob"


def test_read_refuses_a_corrupt_set_with_two_transition_in_cues() -> None:
    state = _state([{"kind": "transition_in", "in_ms": 1}, {"kind": "transition_in", "in_ms": 2}])
    with pytest.raises(cue_store.CueStoreError) as caught:
        cue_store.transition_in_cue(state)
    assert caught.value.code == "TRANSITION_IN_CORRUPT"


def test_save_refuses_a_point_past_the_end() -> None:
    with pytest.raises(cue_store.CueStoreError) as caught:
        cue_store.save_transition_in(_state([]), 300_000, duration_ms=240_000, write=lambda _blob: None)
    assert caught.value.code == "INVALID_CUE_POSITION"


# ----- snapping and the planner ----------------------------------------------


def test_snap_picks_the_nearest_downbeat() -> None:
    downbeats = (0.0, 2.0, 4.0, 6.0)
    assert snap_to_downbeat(downbeats, 4.9) == (3, 4.0)
    assert snap_to_downbeat(downbeats, 5.1) == (4, 6.0)
    assert snap_to_downbeat(downbeats, -3.0) == (1, 0.0)
    assert snap_to_downbeat(downbeats, 99.0) == (4, 6.0)


def test_control_without_a_cue_the_intro_wins() -> None:
    """The old choice, unchanged: no cue means the rule-based ranking."""
    windows = mix_windows(profile(SID), "in")
    assert profile(SID).transition_in_bar is None
    assert (windows[0].label, windows[0].start_bar) == ("intro", 1)
    assert windows[0].pinned is False
    assert len(windows) > 1, "the control must still offer the rule-based candidates"


def test_the_cue_wins_and_is_the_only_in_window() -> None:
    """Bar 17 is the start of the 'up' build, ranked below the intro without the cue."""
    cued = _profile_with_cue(round(16 * BAR_S * 1000))
    assert cued.transition_in_bar == 17
    windows = mix_windows(cued, "in")
    assert [w.start_bar for w in windows] == [17]
    assert windows[0].score == USER_CUE_SCORE
    assert windows[0].pinned is True
    assert windows[0].reason.startswith("your transition-in cue")
    assert windows[0].max_bars == 16  # still stops before the drop at bar 33


def test_an_off_grid_cue_snaps_to_the_nearest_bar() -> None:
    assert _profile_with_cue(round((16 * BAR_S + 0.4 * BAR_S) * 1000)).transition_in_bar == 17
    assert _profile_with_cue(round((16 * BAR_S + 0.6 * BAR_S) * 1000)).transition_in_bar == 18


def test_a_mid_phrase_cue_stays_where_the_user_put_it() -> None:
    windows = mix_windows(_profile_with_cue(round(12 * BAR_S * 1000)), "in")
    assert windows[0].start_bar == 13, "the cue was moved to a phrase start"
    assert windows[0].pinned is True
    assert windows[0].label == "intro"
    assert "mid-intro" in windows[0].reason


def test_propose_transition_keeps_the_cue_bar_except_drop_swap() -> None:
    """[if] a cued in-window would move under place_hit [then] it is refused, not accepted moved, [else stop]."""
    from apps.autodj.propose import propose_transition

    cued = _profile_with_cue(round(12 * BAR_S * 1000))
    assert cued.transition_in_bar == 13
    result = propose_transition(profile("trk-out"), cued, out_deck=1, in_deck=2, intent="energy_up", top=None)
    bad_starts = {17, 25, 26, 29}
    for proposal in result["proposals"]:
        style = proposal["plan"]["style"]
        start = proposal["plan"]["in"]["window"]["start_bar"]
        if style != "drop_swap":
            assert start == 13
        assert start not in bad_starts or style == "drop_swap"
    reasons = " ".join(row["reason"] for row in result["not_offered"])
    assert "transition-in cue" in reasons or "pinned" in reasons
    assert result["not_offered"], "styles that would move the cue should land in not_offered"


def test_propose_transition_without_a_cue_still_moves_in_windows() -> None:
    """[if] there is no transition-in cue [then] place_hit may still move the in-window, [else stop]."""
    from apps.autodj.propose import propose_transition

    result = propose_transition(profile("trk-out"), profile("trk-in"), out_deck=1, in_deck=2, intent="energy_up", top=None)
    rationales = " ".join(p["rationale"] for p in result["proposals"])
    assert "in-window moved" in rationales


def test_propose_transition_accepts_a_cue_that_place_hit_would_keep() -> None:
    """[if] the cue bar already fits hit placement [then] the plan enters on that bar, [else stop]."""
    from apps.autodj.propose import propose_transition

    cued = _profile_with_cue(round(24 * BAR_S * 1000))
    assert cued.transition_in_bar == 25
    result = propose_transition(profile("trk-out"), cued, out_deck=1, in_deck=2, intent="energy_up", top=None)
    kept = [
        p
        for p in result["proposals"]
        if p["plan"]["style"] != "drop_swap" and p["plan"]["in"]["window"]["start_bar"] == 25
    ]
    assert kept, "when place_hit keeps the cue bar, a non-drop_swap plan should enter there"


def test_the_out_role_ignores_the_transition_in_cue() -> None:
    assert mix_windows(_profile_with_cue(0), "out") == mix_windows(profile(SID), "out")


def test_two_transition_in_cues_refuse_the_profile() -> None:
    anlz = anlz_payload(SID)
    anlz["cues"] = [{"kind": "transition_in", "in_ms": 0}, {"kind": "transition_in", "in_ms": 5000}]
    with pytest.raises(ProfileError):
        profile_from_payloads(anlz, track_row(SID))
