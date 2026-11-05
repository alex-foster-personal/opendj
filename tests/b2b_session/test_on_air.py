"""LINKAIR-01: ON AIR is decided once, by the engine that owns the deck.

- [if] a case in the shared conformance vectors [then] the engine rule gives its on_air [else the
  engine and the page's instant local tag disagree]
- [if] the page did not publish a deck's mixer gain (an older page) [then] on_air is None with the
  reason, never a guess
- [if] the now-playing answer is built [then] each deck carries on_air and on_air_reason from the
  owning engine's rule (Speakers and LINK included, which the old peer rule ignored)
- [if] the demo partner's answer is built [then] its mixer decides (it has no room), never the
  room verdict

[if] engine on_air disagrees with conformance vectors [then] fail naming the case, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.b2b_session import on_air
from apps.b2b_session.peer_now_playing import shape_now_playing

pytestmark = [pytest.mark.requirement("LINKAIR-01")]

REPO = Path(__file__).resolve().parents[2]
VECTORS = json.loads((REPO / "apps/webui/frontend/tests/unit/fixtures/on-air-vectors.json").read_text())


def test_the_epsilon_is_the_one_the_vectors_were_written_for() -> None:
    assert VECTORS["epsilon"] == on_air.ON_AIR_GAIN_EPSILON


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["name"] for c in VECTORS["cases"]])
def test_conformance_vectors(case: dict[str, Any]) -> None:
    context = on_air.OnAirContext(
        link_joined=case["link_joined"], speakers_on=case["speakers_on"] if case["speakers_known"] else None
    )
    verdict = on_air.deck_on_air(loaded=case["loaded"], gain=case["gain"], context=context)
    assert verdict.on_air is case["on_air"], f"{case['name']}: {verdict.reason}"
    if not case["speakers_known"] and case["loaded"] and case["link_joined"]:
        assert "not known" in verdict.reason, f"an unknown switch is named as unknown (muted), never as off: {verdict.reason}"
    assert verdict.reason, "every verdict says why"


def test_an_unpublished_gain_is_unknown_never_a_guess() -> None:
    verdict = on_air.deck_on_air(
        loaded=True, gain=None, context=on_air.OnAirContext(link_joined=True, speakers_on=True)
    )
    assert verdict.on_air is None and "mixer gain" in verdict.reason
    unloaded = on_air.deck_on_air(
        loaded=False, gain=None, context=on_air.OnAirContext(link_joined=True, speakers_on=True)
    )
    assert unloaded.on_air is False, "control: nothing loaded is never on air, gain or not"


def test_a_gain_outside_zero_to_one_is_refused() -> None:
    for bad in (-0.1, 1.5, float("nan")):
        with pytest.raises(ValueError):
            on_air.deck_on_air(loaded=True, gain=bad, context=on_air.OnAirContext(link_joined=True, speakers_on=True))


def _mirror(gain: float | None, speakers_on: bool | None) -> dict[str, Any]:
    deck = {
        "stable_id": "sid-1",
        "title": "T",
        "playing": True,
        "position": {"ms": 1000},
        "bpm": 120.0,
        "effective_bpm": 120.0,
    }
    channel: dict[str, Any] = {"fader": 1.0, "assign": "THRU"}
    if gain is not None:
        channel["on_air_gain"] = gain
    mirror: dict[str, Any] = {
        "master_deck": 1,
        "decks": {"1": deck},
        "mixer": {"crossfader": 0.5, "channels": {"1": channel}},
    }
    if speakers_on is not None:
        mirror["room_output"] = {"speakers_on": speakers_on, "room_path": "room_only"}
    return mirror


def test_the_answer_carries_the_owning_engines_verdict() -> None:
    joined = on_air.OnAirContext.from_mirror(_mirror(0.9, True), link_joined=True)
    deck = shape_now_playing(_mirror(0.9, True), 0.1, "Ana", 3.0, joined)["decks"][0]
    assert (deck["on_air"], deck["on_air_reason"]) == (True, on_air.ON_AIR_REASON)
    off = on_air.OnAirContext.from_mirror(_mirror(0.9, False), link_joined=True)
    assert shape_now_playing(_mirror(0.9, False), 0.1, "Ana", 3.0, off)["decks"][0]["on_air"] is False, (
        "Speakers off: not on air"
    )
    old_page = on_air.OnAirContext.from_mirror(_mirror(None, True), link_joined=True)
    assert shape_now_playing(_mirror(None, True), 0.1, "Ana", 3.0, old_page)["decks"][0]["on_air"] is None


def test_the_demo_partner_is_decided_by_its_mixer_alone() -> None:
    demo = on_air.OnAirContext.demo()
    assert demo == on_air.OnAirContext(link_joined=True, speakers_on=True)
    deck = shape_now_playing(_mirror(0.9, None), 0.1, "demo", 3.0, demo)["decks"][0]
    assert deck["on_air"] is True


@pytest.mark.requirement("LINKCLOCK-02")
def test_the_answer_carries_the_audio_clock_watchdog_state() -> None:
    mirror = _mirror(0.9, True)
    context = on_air.OnAirContext.from_mirror(mirror, link_joined=True)
    assert shape_now_playing(mirror, 0.1, "Ana", 3.0, context)["audio_clock"] is None, (
        "an older page: None, never a default"
    )
    mirror["audio_health"] = {
        "clock": {
            "state": "recovering",
            "recovery_step": "rebuild",
            "reason": "clock at 49.8%, rebuilding the audio graph",
        }
    }
    assert shape_now_playing(mirror, 0.1, "Ana", 3.0, context)["audio_clock"] == {
        "state": "recovering",
        "recovery_step": "rebuild",
        "reason": "clock at 49.8%, rebuilding the audio graph",
    }
    mirror["audio_health"] = {"clock": {"state": "melting"}}
    assert shape_now_playing(mirror, 0.1, "Ana", 3.0, context)["audio_clock"] is None, "an unknown state is refused"
