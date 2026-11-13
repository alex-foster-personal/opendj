"""VARIANT-13916: handover mode variants behind ``lighting.handover`` (issue #10109).

[if] a handover mode, its live switch, the phrase_switch hold, or ambient-on-stop breaks [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.lighting.deck_weight import deck_weights
from apps.lighting.runtime import FeedFrame
from apps.lighting.settings import HANDOVER_KEY, SETTINGS
from apps.lighting.show_runtime import AMBIENT_FRAME, DeckPlan, DeckState, HandoverMode, ShowCue, ShowState, step
from tests.lighting.test_palettes import _client

pytestmark = [pytest.mark.requirement("VARIANT-13916")]

BEATS = 64
PLAN_A = DeckPlan("ta", (ShowCue(0, "a-red", 1),), (0, 32))
PLAN_B = DeckPlan("tb", (ShowCue(0, "b-blue", 1),), (0, 20, 40, 60))


def _weights(beat: int, mode: HandoverMode) -> dict[str, float]:
    xf = beat / (BEATS - 1)
    fader_a = 1.0 if beat < 32 else 1.0 - (beat - 32) / 31
    channels = [{"deck_id": 1, "fader": fader_a, "assign": "A"}, {"deck_id": 2, "fader": 1.0, "assign": "B"}]
    weight_mode = "blend" if mode is HandoverMode.PHRASE_SWITCH else mode.value
    raw = deck_weights(channels, xf, 1 if beat < 44 else 2, weight_mode)
    return {"A": raw[1], "B": raw[2]}


def _decks(beat: int, weights: dict[str, float], playing: bool = True) -> list[DeckState]:
    bar_beat = beat % 4 + 1
    return [
        DeckState("A", playing, beat, bar_beat, weights["A"], PLAN_A),
        DeckState("B", playing, beat, bar_beat, weights["B"], PLAN_B),
    ]


def _timeline(mode: HandoverMode) -> list[str | None]:
    state, out = ShowState(), []
    for beat in range(BEATS):
        state, frame = step(state, _decks(beat, _weights(beat, mode)), mode)
        out.append(frame.colour_deck)
    return out


def test_replayed_crossfade_differs_per_mode_and_phrase_switch_lands_on_boundary() -> None:
    """[if] the 16-bar crossfade timelines match across modes or phrase_switch misses beat 40 [then] fail, [else stop]."""
    first_b = {mode: _timeline(mode).index("B") for mode in HandoverMode}
    assert first_b == {
        HandoverMode.BLEND: 32,
        HandoverMode.UPFADER_ONLY: 36,
        HandoverMode.PHRASE_SWITCH: 40,
        HandoverMode.MASTER_ONLY: 60,
    }
    state = ShowState()
    for beat in range(42):
        state, frame = step(state, _decks(beat, _weights(beat, HandoverMode.PHRASE_SWITCH)), HandoverMode.PHRASE_SWITCH)
        if beat == 40:
            assert [(lay.deck, lay.intensity) for lay in frame.layers] == [("A", 0.5), ("B", 0.5)]
        if beat == 41:
            assert [(lay.deck, lay.intensity) for lay in frame.layers] == [("B", 1.0)]


def test_live_handover_set_applies_on_next_downbeat_and_logs(tmp_path: Path) -> None:
    """[if] handover set to phrase_switch mid-mix applies before the next downbeat or skips the pick log [then] fail, [else stop]."""
    client, runtime, log = _client(tmp_path)
    response = client.put("/api/v1/lighting/settings/handover", json={"value": "phrase_switch"})
    assert response.status_code == 200
    assert response.json()["previous"] == "blend"
    assert runtime.show.mode is HandoverMode.BLEND
    real_ingest = runtime.ingest
    runtime.ingest = lambda frame: [{"type": "beat", "is_downbeat": False}]  # type: ignore[method-assign]
    runtime.process(FeedFrame(decks=(), weights={}, host_time_ns=1))
    assert runtime.show.mode is HandoverMode.BLEND
    runtime.ingest = lambda frame: [{"type": "beat", "is_downbeat": True}]  # type: ignore[method-assign]
    runtime.process(FeedFrame(decks=(), weights={}, host_time_ns=2))
    runtime.ingest = real_ingest  # type: ignore[method-assign]
    assert runtime.show.mode is HandoverMode.PHRASE_SWITCH
    line = json.loads(log.read_text().splitlines()[-1])
    assert (line["key"], line["from"], line["to"]) == (HANDOVER_KEY, "blend", "phrase_switch")


def test_phrase_switch_flips_back_only_after_four_beats_above_half() -> None:
    """[if] phrase_switch flips back before the outgoing deck holds weight above 0.5 for 4 beats [then] fail, [else stop]."""
    mode = HandoverMode.PHRASE_SWITCH
    state = ShowState()
    for beat in range(41):
        state, _ = step(state, _decks(beat, {"A": 0.2, "B": 0.8} if beat >= 20 else {"A": 0.8, "B": 0.2}), mode)
    assert state.owner == "B"
    beat = 41
    for weights, count in (({"A": 0.7, "B": 0.3}, 3), ({"A": 0.3, "B": 0.7}, 1), ({"A": 0.7, "B": 0.3}, 5)):
        for _ in range(count):
            state, frame = step(state, _decks(beat, weights), mode)
            owners = frame.colour_deck
            beat += 1
            if beat <= 49:
                assert owners == "B"
    assert state.owner == "A"


def test_handover_value_outside_four_answers_400() -> None:
    """[if] PUT handover with an unknown value is not 400 naming the four modes [then] fail, [else stop]."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        client, _runtime, _log = _client(Path(tmp))
        response = client.put("/api/v1/lighting/settings/handover", json={"value": "crossfade"})
    assert response.status_code == 400
    assert response.json()["detail"]["allowed"] == ["blend", "upfader_only", "master_only", "phrase_switch"]
    assert SETTINGS[HANDOVER_KEY].allowed == tuple(m.value for m in HandoverMode)


@pytest.mark.parametrize("mode", list(HandoverMode))
def test_every_mode_goes_ambient_when_both_decks_stop(mode: HandoverMode) -> None:
    """[if] both decks stop and a mode does not return the ambient frame [then] fail, [else stop]."""
    state, _ = step(ShowState(), _decks(0, {"A": 0.6, "B": 0.4}), mode)
    state, frame = step(state, _decks(1, {"A": 0.6, "B": 0.4}, playing=False), mode)
    assert frame == AMBIENT_FRAME
    assert state == ShowState()


@pytest.mark.parametrize("mode", [HandoverMode.BLEND, HandoverMode.UPFADER_ONLY])
def test_first_owner_after_ambient_holds_colour_four_beats(mode: HandoverMode) -> None:
    """[if] the first deck after ambient loses colour on a downbeat inside its 4-beat hold [then] fail, [else stop]."""
    state, frame = step(ShowState(), _decks(100, {"A": 0.6, "B": 0.4}, playing=False), mode)
    assert frame == AMBIENT_FRAME
    colours: dict[int, str | None] = {}
    for beat in range(101, 109):
        weights = {"A": 0.6, "B": 0.4} if beat == 101 else {"A": 0.3, "B": 0.7}
        state, frame = step(state, _decks(beat, weights), mode)
        colours[beat] = frame.colour_deck
    assert colours == {**dict.fromkeys(range(101, 108), "A"), 108: "B"}
