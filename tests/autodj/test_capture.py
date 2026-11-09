"""MEMORY-04: a hand mix read from the live mirror becomes one captured mix.

[if] a second deck plays and the fader moves until the first stops [then] one mix closes with those lanes, [else stop].
[if] a plan or AutoPlay is running [then] nothing is captured, [else stop].
[if] a deck rewinds [then] that attempt is dropped and the next one opens, [else stop].
[if] a loop is engaged [then] the overlap is not captured, and says so, [else stop].
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.capture import SILENT_HOLD_BARS, ClosedMix, PreGridIncomingError, TransitionCapture, in_bars
from apps.autodj.profile import TrackProfile
from apps.autodj.simplify import CapturedMix, Sample, SimplifyError, simplify
from apps.webui.server.pair_capture import finish_capture

pytestmark = pytest.mark.requirement("MEMORY-04")

BPM = 124.0
BAR_S = 240.0 / BPM
FIRST_DOWNBEAT_S = 0.5


def _channel(**levels: float) -> dict[str, Any]:
    return {
        "fader": 1.0,
        "trim": 0.5,
        "filter": 0.5,
        "eq_low": 0.5,
        "eq_mid": 0.5,
        "eq_high": 0.5,
        "assign": "THRU",
        **levels,
    }


def profile(stable_id: str, *, bars: int = 200) -> TrackProfile:
    """A grid with its first downbeat half a second in: a track's grid never has to start at zero."""
    return TrackProfile(
        stable_id=stable_id,
        title=None,
        artist=None,
        bpm=BPM,
        key=None,
        duration_s=FIRST_DOWNBEAT_S + bars * BAR_S,
        downbeats_s=tuple(FIRST_DOWNBEAT_S + index * BAR_S for index in range(bars)),
        grid_trusted=True,
        grid_note=None,
        sections=(),
        sections_source="none",
        bar_energy=None,
        bar_bass=None,
        bar_vocals=None,
        vocals_status="missing",
    )


def _deck(track: str | None, bar: float, *, playing: bool = True, loop: bool = False) -> dict[str, Any]:
    """A deck as ``ui-mirror.ts`` publishes it: ``bars_beats`` comes from ANLZ beat numbers, so it stays in bar 1."""
    return {
        "stable_id": track,
        "playing": playing,
        "bpm": BPM,
        "effective_bpm": BPM,
        "position": {
            "ms": (FIRST_DOWNBEAT_S + (bar - 1) * BAR_S) * 1000.0,
            "bars_beats": f"1.{int((bar - int(bar)) * 4) + 1}",
            "phrase": 1,
        },
        "sync": {"mode": "off", "enabled": True},
        "loop": {"engaged": True} if loop else None,
    }


def _mirror(
    out: dict[str, Any], in_: dict[str, Any], *, out_fader: float = 1.0, in_fader: float = 0.0, **top: Any
) -> dict[str, Any]:
    return {
        "master_deck": 1,
        "agent_plan": None,
        "autoplay_enabled": False,
        "decks": {"1": out, "2": in_},
        "mixer": {
            "crossfader": 0.5,
            "channels": {"1": _channel(fader=out_fader), "2": _channel(fader=in_fader)},
        },
        **top,
    }


def _crossfade(capture: TransitionCapture, *, bars: int = 16, start: float = 65.0) -> None:
    """Deck 1 alone, then deck 2 enters and the faders swap, two mirrors a bar."""
    capture.observe(_mirror(_deck("out", start - 1), _deck("in", 1.0, playing=False)))
    for half in range(bars * 2 + 1):
        t = half / (bars * 2)
        at = half / 2
        capture.observe(_mirror(_deck("out", start + at), _deck("in", 1.0 + at), out_fader=1.0 - t, in_fader=t))


def _stop(capture: TransitionCapture, bar: float = 81.5) -> ClosedMix | None:
    return capture.observe(_mirror(_deck("out", bar, playing=False), _deck("in", 17.5), out_fader=0.0, in_fader=1.0))


def _on_plan_clock(mix: ClosedMix | None) -> CapturedMix:
    assert mix is not None
    return in_bars(mix, profile(mix.out_track), profile(mix.in_track))


def test_a_hand_crossfade_closes_into_one_mix_when_the_outgoing_deck_stops() -> None:
    capture = TransitionCapture()
    _crossfade(capture)
    assert capture.status()["state"] == "capturing"
    mix = _on_plan_clock(_stop(capture))
    assert (mix.out_track, mix.in_track, mix.out_deck, mix.in_deck) == ("out", "in", 1, 2)
    assert (mix.out_start_bar, mix.in_start_bar) == (65, 1)
    plan = simplify(mix)
    assert plan.to_json()["out"]["track"] == "out"
    assert [e.name for e in plan.events if e.kind == "ramp"] == ["fader", "fader"]
    assert capture.status() == {
        "state": "idle",
        "out": None,
        "in": None,
        "samples": 0,
        "last": {"status": "closed", "reason": "outgoing_stopped", "out": "out", "in": "in"},
    }


def test_the_incoming_bar_lines_up_with_the_outgoing_downbeat() -> None:
    """Seen mid-bar: the deck started about half a bar ago, so bar 0 pairs with its first bar, not its second."""
    capture = TransitionCapture()
    capture.observe(_mirror(_deck("out", 64.5), _deck("in", 1.0, playing=False)))
    capture.observe(_mirror(_deck("out", 65.5), _deck("in", 33.5)))
    for half in range(1, 20):
        capture.observe(_mirror(_deck("out", 65.5 + half / 2), _deck("in", 33.5 + half / 2), out_fader=1 - half / 20))
    mix = _on_plan_clock(_stop(capture, 75.5))
    assert (mix.out_start_bar, mix.in_start_bar) == (65, 33)


@pytest.mark.parametrize(
    "automation",
    [
        {"agent_plan": {"id": "p", "status": "running"}},
        {"agent_plan": {"id": "p", "status": "scheduled"}},
        {"autoplay_enabled": True},
    ],
)
def test_nothing_is_captured_while_a_plan_or_autoplay_runs(automation: dict[str, Any]) -> None:
    capture = TransitionCapture()
    for half in range(8):
        capture.observe(_mirror(_deck("out", 65 + half / 2), _deck("in", 1 + half / 2), **automation))
    assert capture.status()["state"] == "idle"


def test_a_plan_starting_mid_overlap_abandons_and_the_tail_is_not_captured() -> None:
    capture = TransitionCapture()
    _crossfade(capture)
    capture.observe(_mirror(_deck("out", 81.5), _deck("in", 17.5), agent_plan={"id": "p", "status": "running"}))
    assert capture.status()["last"]["reason"] == "automation"
    capture.observe(_mirror(_deck("out", 82.0), _deck("in", 18.0), agent_plan={"id": "p", "status": "done"}))
    assert capture.status()["state"] == "idle"


def test_a_rewind_drops_the_attempt_and_the_retry_is_captured_instead() -> None:
    capture = TransitionCapture()
    _crossfade(capture, bars=8)
    capture.observe(_mirror(_deck("out", 65.0), _deck("in", 1.0), out_fader=1.0, in_fader=0.0))
    assert capture.status()["state"] == "capturing"
    assert capture.status()["last"]["reason"] == "rewound"
    assert capture.status()["samples"] == 1
    for half in range(1, 9):
        capture.observe(
            _mirror(_deck("out", 65 + half / 2), _deck("in", 1 + half / 2), out_fader=1 - half / 8, in_fader=half / 8)
        )
    mix = _on_plan_clock(_stop(capture, 69.5))
    assert (mix.out_start_bar, len(mix.lanes[(1, "fader")])) == (65, 9)


def test_the_incoming_deck_stopping_is_no_transition() -> None:
    capture = TransitionCapture()
    _crossfade(capture, bars=4)
    closing = capture.observe(_mirror(_deck("out", 69.5), _deck("in", 5.5, playing=False)))
    assert closing is None
    assert capture.status()["last"]["reason"] == "incoming_stopped"


@pytest.mark.parametrize("looped", ["out", "in"])
def test_a_loop_abandons_the_overlap_until_the_pair_stops(looped: str) -> None:
    capture = TransitionCapture()
    _crossfade(capture, bars=4)
    capture.observe(_mirror(_deck("out", 69.5, loop=looped == "out"), _deck("in", 5.5, loop=looped == "in")))
    assert capture.status()["last"]["reason"] == "loop_engaged"
    capture.observe(_mirror(_deck("out", 70.0), _deck("in", 6.0)))
    assert capture.status()["state"] == "idle"


def test_a_faded_out_deck_left_running_closes_after_the_silent_hold_at_the_bar_it_went_quiet() -> None:
    capture = TransitionCapture()
    _crossfade(capture, bars=8)  # the fader reaches 0 at plan bar 8
    closed: ClosedMix | None = None
    for half in range(1, int(SILENT_HOLD_BARS * 2) + 2):
        closed = closed or capture.observe(
            _mirror(_deck("out", 73 + half / 2), _deck("in", 9 + half / 2), out_fader=0.0, in_fader=1.0)
        )
    mix = _on_plan_clock(closed)
    assert mix.end_bar == pytest.approx(8.0)
    assert simplify(mix).bars == 8
    assert capture.status()["last"]["reason"] == "outgoing_silent"
    capture.observe(_mirror(_deck("out", 80.0), _deck("in", 16.0), out_fader=0.0, in_fader=1.0))
    assert capture.status()["state"] == "idle"  # not reopened on the same pair


def test_the_same_track_on_both_decks_is_no_pair() -> None:
    capture = TransitionCapture()
    capture.observe(_mirror(_deck("out", 10.0), _deck("out", 20.0)))
    assert capture.status()["state"] == "idle"


def test_a_malformed_mirror_raises_rather_than_recording_a_zero() -> None:
    capture = TransitionCapture()
    capture.observe(_mirror(_deck("out", 64.5), _deck("in", 1.0, playing=False)))
    broken = _mirror(_deck("out", 65.0), _deck("in", 1.0))
    broken["mixer"]["channels"]["2"]["fader"] = None
    with pytest.raises(ValueError, match="deck 2 fader"):
        capture.observe(broken)


def test_the_repeating_beat_numbers_of_a_real_mirror_are_no_rewind() -> None:
    """Regression: ``bars_beats`` reads 1.1 to 1.4 for every bar of a real track, so it can never be the clock."""
    capture = TransitionCapture()
    _crossfade(capture)
    assert capture.status()["last"] is None  # no bar boundary read as a rewind
    assert capture.status()["samples"] == 33


def test_pitch_does_not_stretch_the_rewind_window_or_the_clock() -> None:
    """position.ms is track time, so the plan bars come from the grid whatever the deck's pitch was."""
    capture = TransitionCapture()
    capture.observe(_mirror(_deck("out", 64.0), _deck("in", 1.0, playing=False)))
    for half in range(17):
        out = {**_deck("out", 65 + half / 2), "effective_bpm": BPM * 1.06}
        capture.observe(_mirror(out, _deck("in", 1 + half / 2), out_fader=1 - half / 16, in_fader=half / 16))
    mix = _on_plan_clock(_stop(capture, 73.5))
    assert [round(sample.at_bar, 6) for sample in mix.lanes[(1, "fader")]][:3] == [0.0, 0.5, 1.0]


def test_a_new_track_on_the_first_deck_is_not_the_one_that_was_there_first() -> None:
    capture = TransitionCapture()
    capture.observe(_mirror(_deck("old", 64.0), _deck("next", 1.0, playing=False)))
    capture.observe(_mirror(_deck("old", 64.5, playing=False), _deck("next", 33.0)))
    capture.observe(_mirror(_deck("fresh", 1.0), _deck("next", 33.5)))
    status = capture.status()
    assert (status["out"], status["in"]) == ("next", "fresh")


def test_a_deck_already_looping_never_opens_a_session() -> None:
    capture = TransitionCapture()
    capture.observe(_mirror(_deck("out", 65.0, loop=True), _deck("in", 1.0)))
    assert capture.status()["state"] == "idle"


def test_both_decks_stopping_together_is_no_transition() -> None:
    capture = TransitionCapture()
    _crossfade(capture, bars=4)
    closing = capture.observe(_mirror(_deck("out", 69.5, playing=False), _deck("in", 5.5, playing=False)))
    assert closing is None
    assert capture.status()["last"]["reason"] == "incoming_stopped"


# ----- PAIR-9950: a slow save's verdict lands only on its own mix ---------------------------------------------------


def _second_mix(capture: TransitionCapture) -> ClosedMix | None:
    """After :func:`_stop`, a fresh track starts on deck 1 against the still-playing deck 2, which then stops."""
    capture.observe(_mirror(_deck("c", 1.0), _deck("in", 18.0), out_fader=0.0, in_fader=1.0))
    for half in range(1, 5):
        capture.observe(_mirror(_deck("c", 1.0 + half / 2), _deck("in", 18.0 + half / 2), out_fader=half / 4))
    return capture.observe(_mirror(_deck("c", 3.5), _deck("in", 20.5, playing=False), out_fader=1.0))


@pytest.mark.requirement("PAIR-9950")
def test_each_close_carries_a_monotonic_capture_id_on_its_mix_and_outcome() -> None:
    capture = TransitionCapture()
    _crossfade(capture)
    first = _stop(capture)
    assert first is not None and capture.last_outcome is not None
    assert first.capture_id == capture.last_outcome.capture_id
    second = _second_mix(capture)
    assert second is not None and (second.out_track, second.in_track) == ("in", "c")
    assert second.capture_id > first.capture_id
    assert capture.last_outcome.capture_id == second.capture_id


@pytest.mark.requirement("PAIR-9950")
def test_an_older_mix_verdict_never_lands_on_a_newer_capture() -> None:
    capture = TransitionCapture()
    _crossfade(capture)
    mix_a = _stop(capture)
    mix_b = _second_mix(capture)
    assert mix_a is not None and mix_b is not None
    capture.note_result(mix_a.capture_id, "saved", "blend")
    assert capture.status()["last"] == {"status": "closed", "reason": "outgoing_stopped", "out": "in", "in": "c"}
    capture.note_result(mix_b.capture_id, "refused", "too short")
    assert capture.status()["last"] == {"status": "refused", "reason": "too short", "out": "in", "in": "c"}


@pytest.mark.requirement("PAIR-9950")
def test_a_mirror_fault_after_a_close_is_never_overwritten_by_the_older_save(caplog: pytest.LogCaptureFixture) -> None:
    capture = TransitionCapture()
    _crossfade(capture)
    mix = _stop(capture)
    assert mix is not None
    capture.note_fault("deck 2 fader in the live mirror is None, not a number")
    with caplog.at_level("INFO", logger="apps.autodj.capture"):
        capture.note_result(mix.capture_id, "saved", "blend")
    last = capture.status()["last"]
    assert (last["status"], last["reason"]) == ("failed", "deck 2 fader in the live mirror is None, not a number")
    assert any("dropped the saved verdict" in record.getMessage() for record in caplog.records)


@pytest.mark.requirement("PAIR-9950")
def test_a_verdict_for_the_current_mix_still_lands() -> None:
    capture = TransitionCapture()
    _crossfade(capture)
    mix = _stop(capture)
    assert mix is not None
    capture.note_result(mix.capture_id, "saved", "blend")
    assert capture.status()["last"]["status"] == "saved"


# ----- PAIR-9954: an incoming started before its grid's bar 1 ---------------------------------


def _pre_grid_mix(in_start_s: float) -> ClosedMix:
    """Deck 2 entered at ``in_start_s`` track seconds, lined up with outgoing bar 65's downbeat."""
    out_start_s = FIRST_DOWNBEAT_S + 64 * BAR_S
    return ClosedMix(
        capture_id=1,
        out_deck=1,
        out_track="out",
        out_start_s=out_start_s,
        in_deck=2,
        in_track="in",
        in_start_s=in_start_s,
        end_s=out_start_s + 16 * BAR_S,
        lanes={(2, "fader"): (Sample(out_start_s, 0.0), Sample(out_start_s + 8 * BAR_S, 1.0))},
        tempo_mode="sync_to_out",
    )


def _late_grid_profile(stable_id: str) -> TrackProfile:
    """An incoming whose grid starts 4 bars in: a pickup or intro the grid does not cover."""
    return replace(profile(stable_id), downbeats_s=tuple(4 * BAR_S + index * BAR_S for index in range(200)))


@pytest.mark.requirement("PAIR-9954")
def test_an_incoming_started_before_its_grid_is_refused_naming_the_projected_bar() -> None:
    """[if] the projected incoming bar is below 1 [then] a typed refusal names it, never a silent bar 1."""
    with pytest.raises(PreGridIncomingError, match=r"projected bar -1, before its grid's bar 1") as refused:
        in_bars(_pre_grid_mix(in_start_s=2 * BAR_S), profile("out"), _late_grid_profile("in"))
    assert isinstance(refused.value, SimplifyError)
    assert refused.value.projected_bar == -1


@pytest.mark.requirement("PAIR-9954")
def test_an_incoming_on_or_after_bar_1_is_unchanged() -> None:
    """[if] the projected incoming bar is 1 or more [then] it is kept as projected."""
    mix = in_bars(_pre_grid_mix(in_start_s=4 * BAR_S), profile("out"), _late_grid_profile("in"))
    assert (mix.out_start_bar, mix.in_start_bar) == (65, 1)


@pytest.mark.requirement("PAIR-9954")
def test_a_pre_grid_capture_reads_not_saved_and_writes_nothing(tmp_path: Path) -> None:
    """[if] a closed mix's incoming started before its grid [then] the status is not_saved with that reason and no
    played record is written."""
    capture = TransitionCapture()
    _crossfade(capture)
    assert _stop(capture) is not None
    db_path = tmp_path / "state.db"
    profiles = {"out": profile("out"), "in": _late_grid_profile("in")}
    finish_capture(_pre_grid_mix(in_start_s=2 * BAR_S), capture, db_path, profiles.__getitem__)
    last = capture.status()["last"]
    assert last["status"] == "not_saved"
    assert "projected bar -1" in last["reason"]
    assert not db_path.exists(), "a refused capture must not open the state database"
