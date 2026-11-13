"""Two-deck lanes for a try-er render (TRYER-16): waveform peaks and knob curves on the render clock.

Pure: profiles from the AutoDJ fixtures, plans from the real proposer, deck renders written as 16-bit WAV here.
"""

from __future__ import annotations

import dataclasses
import wave
from pathlib import Path

import numpy as np
import pytest

from apps.autodj.http import build_service
from apps.autodj.plan import parse_plan
from apps.autodj.profile import heard_profile, profile_from_payloads
from apps.autodj.propose import propose_transition
from apps.autodj.render_plan import compile_render_plan, layout_of
from apps.transition_score.lanes import (
    POWER_ON,
    LaneError,
    beat_grid,
    build_lanes,
    deck_peaks,
    knob_curves,
    lane_energy,
    playing_spans,
)
from tests.autodj.fixtures import anlz_payload, profile, track_row
from tests.transition_score.arrangements import ARRANGEMENT, IN_ARRANGEMENT

ANCHOR = Path(Path.cwd().anchor)


def _case(style: str):  # type: ignore[no-untyped-def]
    out_profile = profile("fixture-out", bpm=124.0, arrangement=ARRANGEMENT)
    in_profile = profile("fixture-in", bpm=124.0, key="9A", arrangement=IN_ARRANGEMENT)
    proposals = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, top=12)["proposals"]
    plan = next(parse_plan(p["plan"]) for p in proposals if p["plan"]["style"] == style)
    layout = layout_of(plan, out_profile, in_profile, 4)
    document = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=ANCHOR, in_audio=ANCHOR)
    return plan, out_profile, layout, document


def _rise_start(fader: list[tuple[float, float]]) -> float:
    """Time of the last 0.0 breakpoint before the incoming fader first opens (the kill is hoisted to t0, #12872)."""
    rise = next(i for i, (_, v) in enumerate(fader) if v > 0.0 and any(w == 0.0 for _, w in fader[:i]))
    return next(t for t, v in reversed(fader[:rise]) if v == 0.0)


def _write_wav(path: Path, samples: np.ndarray, rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())


def test_peaks_are_full_scale_fractions_never_renormalized(tmp_path: Path) -> None:
    quiet = np.concatenate([np.zeros(48000), 0.25 * np.ones(48000)])
    _write_wav(tmp_path / "d.wav", quiet)
    peaks, duration = deck_peaks(tmp_path / "d.wav", 4)
    assert duration == pytest.approx(2.0), "if the duration is not samples / rate then the lanes misplace every line"
    assert peaks[:2] == [0.0, 0.0], "if silence reads above 0 then the waveform invents audio"
    assert peaks[2] == pytest.approx(0.25, abs=1e-3), "if a quarter-scale signal is renormalized to 1 then levels lie"


def test_columns_out_of_range_are_refused(tmp_path: Path) -> None:
    _write_wav(tmp_path / "d.wav", np.zeros(100))
    with pytest.raises(LaneError):
        deck_peaks(tmp_path / "d.wav", 0)


def test_incoming_fader_curve_rises_from_zero_on_the_render_clock() -> None:
    plan, out_profile, layout, document = _case("crossfade")
    curves = knob_curves(document, plan.out.deck, out_profile, layout)
    fader = curves[plan.in_.deck]["fader"]
    assert fader[0] == (0.0, POWER_ON["fader"]), "if the curve does not start at the power-on value then t0 is wrong"
    values = [v for _, v in fader]
    assert 0.0 in values, "if the incoming fader never reads 0 then the set at bar 0 was dropped"
    assert values[-1] == pytest.approx(1.0), "if the incoming fader does not end at unity then a ramp was lost"
    times = [t for t, _ in fader]
    assert times == sorted(times), "if breakpoints go backwards in time then events were placed on the wrong clock"
    first_zero = _rise_start(fader)
    expected = (out_profile.time_of_bar(plan.out.start_bar) - layout.start_out_s) / layout.out_ratio
    assert first_zero == pytest.approx(expected, abs=1e-3), "if bar 0 is not the plan's first bar then lines drift"


def test_outgoing_fader_falls_to_zero_and_untouched_knobs_stay_put() -> None:
    plan, out_profile, layout, document = _case("crossfade")
    curves = knob_curves(document, plan.out.deck, out_profile, layout)
    assert min(v for _, v in curves[plan.out.deck]["fader"]) == pytest.approx(0.0), (
        "if the outgoing fader never reaches 0 then the fade-out is not drawn"
    )
    filter_values = {v for _, v in curves[plan.out.deck].get("filter", [(0.0, POWER_ON["filter"])])}
    assert filter_values == {POWER_ON["filter"]}, "if a crossfade's filter line leaves neutral then lines are invented"


def test_a_ramp_on_another_clock_is_refused_not_skipped() -> None:
    plan, out_profile, layout, document = _case("crossfade")
    ramp = next(e for e in document["events"] if "ramp" in e)
    ramp["ramp"]["over"]["deck"] = plan.in_.deck
    with pytest.raises(LaneError):
        knob_curves(document, plan.out.deck, out_profile, layout)


def test_build_lanes_reads_both_decks_and_their_play_spans(tmp_path: Path) -> None:
    plan, out_profile, layout, document = _case("crossfade")
    for deck in (1, 2):
        _write_wav(tmp_path / "decks" / f"deck{deck}.wav", 0.5 * np.ones(48000 * 30))
    _write_wav(tmp_path / "decks" / f"deck{plan.in_.deck + 1}.wav", 0.5 * np.ones(48000 * 30))
    lanes = build_lanes(tmp_path, document, plan.out.deck, out_profile, layout, 50)
    assert [lane.deck for lane in lanes.decks] == [1, 2]
    incoming = lanes.decks[1]
    assert incoming.playing and incoming.playing[0][0] > 0.0, (
        "if the incoming deck plays from t0 then its play command was not read"
    )
    assert lanes.decks[0].playing[0][0] == 0.0


def test_build_lanes_without_deck_renders_fails_loud(tmp_path: Path) -> None:
    plan, out_profile, layout, document = _case("crossfade")
    with pytest.raises(LaneError, match=r"deck1\.wav.*deck2\.wav"):
        build_lanes(tmp_path, document, plan.out.deck, out_profile, layout, 50)


def test_a_render_missing_the_incoming_deck_is_refused_naming_it(tmp_path: Path) -> None:
    plan, out_profile, layout, document = _case("crossfade")
    _write_wav(tmp_path / "decks" / f"deck{plan.out.deck}.wav", 0.5 * np.ones(48000 * 30))
    with pytest.raises(LaneError, match=rf"deck{plan.in_.deck}\.wav"):
        build_lanes(tmp_path, document, plan.out.deck, out_profile, layout, 50)


def test_incoming_beat_grid_starts_on_its_downbeat_at_its_play_time_and_runs_at_the_out_tempo() -> None:
    plan, out_profile, layout, document = _case("crossfade")
    spans = playing_spans(document, plan.out.deck, out_profile, layout, 60.0)
    grids = beat_grid(document, spans)
    first_t, first_n = grids[plan.in_.deck][0]
    assert first_t == pytest.approx(spans[plan.in_.deck][0][0], abs=1e-3), (
        "if the incoming grid does not start where the deck starts then the bar lines lie"
    )
    assert first_n == 1, "if the incoming deck enters off a downbeat here then the seek was not read"
    downbeats = [t for t, n in grids[plan.in_.deck] if n == 1]
    assert downbeats[1] - downbeats[0] == pytest.approx(out_profile.bar_s, abs=1e-3), (
        "if incoming bars are not stretched to the outgoing tempo then the tempo ratio was not applied"
    )
    out_downbeats = {round(t, 2) for t, n in grids[plan.out.deck] if n == 1}
    assert round(downbeats[0], 2) in out_downbeats, "if the decks' bar lines do not meet then the grids are misplaced"


def _seconds(bar: float, out_profile, layout) -> float:
    return round(out_profile.time_of_bar(bar) - layout.start_out_s, 4)


@pytest.mark.requirement("TRYER-16")
def test_knob_curves_set_interrupts_ramp_truncates_future_endpoint() -> None:
    """[if] an 8-bar fader ramp is cut at bar 4 by a set [then] times stay sorted and bar 4 is 0.5 then 0.8, [else stop]."""
    plan, out_profile, layout, _full_document = _case("crossfade")
    out_deck = plan.out.deck
    b0 = layout.start_out_bar
    document = {
        "events": [
            {
                "at": {"deck": out_deck, "bar": b0},
                "ramp": {"type": "fader", "deck": out_deck, "to": 0.0, "over": {"deck": out_deck, "bars": 8}},
            },
            {
                "at": {"deck": out_deck, "bar": b0 + 4},
                "cmd": {"type": "fader", "deck": out_deck, "value": 0.8},
            },
        ]
    }
    curves = knob_curves(document, plan.out.deck, out_profile, layout)
    fader = curves[out_deck]["fader"]
    times = [t for t, _ in fader]
    assert times == sorted(times), "if breakpoints go backwards in time then interrupted ramps were not truncated"
    t4 = _seconds(b0 + 4, out_profile, layout)
    at4 = [(t, v) for t, v in fader if t == t4]
    assert len(at4) >= 2, "if bar 4 has no jump then the set was not applied at the ramp interrupt"
    assert at4[0][1] == pytest.approx(0.5), (
        "if the value at bar 4 is not half the ramp then the curve did not interpolate at the interrupt"
    )
    assert at4[-1][1] == pytest.approx(0.8), "if the set value is missing at bar 4 then the jump was lost"
    t8 = _seconds(b0 + 8, out_profile, layout)
    assert not any(abs(t - t8) < 1e-6 and v == pytest.approx(0.0) for t, v in fader), (
        "if the cancelled ramp end (0.0) remains at the original end bar then the superseded endpoint was kept"
    )


@pytest.mark.requirement("TRYER-16")
def test_knob_curves_ramp_interrupts_ramp_starts_from_interpolated_value() -> None:
    """[if] a second ramp interrupts the first at bar 4 [then] the new ramp starts from 0.5, [else stop]."""
    plan, out_profile, layout, _full_document = _case("crossfade")
    out_deck = plan.out.deck
    b0 = layout.start_out_bar
    document = {
        "events": [
            {
                "at": {"deck": out_deck, "bar": b0},
                "ramp": {"type": "fader", "deck": out_deck, "to": 0.0, "over": {"deck": out_deck, "bars": 8}},
            },
            {
                "at": {"deck": out_deck, "bar": b0 + 4},
                "ramp": {"type": "fader", "deck": out_deck, "to": 1.0, "over": {"deck": out_deck, "bars": 2}},
            },
        ]
    }
    curves = knob_curves(document, plan.out.deck, out_profile, layout)
    fader = curves[out_deck]["fader"]
    times = [t for t, _ in fader]
    assert times == sorted(times), "if breakpoints go backwards in time then interrupted ramps were not truncated"
    t4 = _seconds(b0 + 4, out_profile, layout)
    t6 = _seconds(b0 + 6, out_profile, layout)
    t8 = _seconds(b0 + 8, out_profile, layout)
    assert any(abs(t - t4) < 1e-6 and v == pytest.approx(0.5) for t, v in fader), (
        "if bar 4 does not read 0.5 then the second ramp did not start from the interrupted ramp level"
    )
    assert any(abs(t - t6) < 1e-6 and v == pytest.approx(1.0) for t, v in fader), (
        "if the second ramp does not end at 1.0 at bar 6 then its endpoint was misplaced"
    )
    assert not any(abs(t - t8) < 1e-6 and v == pytest.approx(0.0) for t, v in fader), (
        "if the first ramp's far endpoint 0.0 remains at bar 8 then the superseded endpoint was not dropped"
    )


@pytest.mark.requirement("TRYER-16")
def test_knob_curves_fixture_curves_have_non_decreasing_times() -> None:
    """[if] crossfade fixture knob curves are built [then] every knob has non-decreasing times, [else stop]."""
    plan, out_profile, layout, document = _case("crossfade")
    curves = knob_curves(document, plan.out.deck, out_profile, layout)
    for deck_curves in curves.values():
        for points in deck_curves.values():
            times = [t for t, _ in points]
            assert times == sorted(times), (
                "if breakpoints go backwards in time then events were placed on the wrong clock or ramps not truncated"
            )


def test_a_tempo_change_mid_render_refuses_the_grid() -> None:
    plan, out_profile, layout, document = _case("crossfade")
    later = next(e for e in document["events"] if e["at"] != {"ms": 0})
    document["events"].append({"at": later["at"], "cmd": {"type": "tempo", "deck": plan.in_.deck, "ratio": 1.01}})
    spans = playing_spans(document, plan.out.deck, out_profile, layout, 60.0)
    with pytest.raises(LaneError):
        beat_grid(document, spans)


def test_lanes_follow_the_jobs_shared_start_not_the_plans_own() -> None:
    """Every render of a job starts at its earliest plan's start bar; a plan that starts later is drawn later by
    exactly the bars between (Sentry on #7806: the lanes used each plan's own start and drew c4 a bar early)."""
    from apps.transition_score.tryer import shared_start_out_bar

    plan, out_profile, layout, document = _case("crossfade")
    own = knob_curves(document, plan.out.deck, out_profile, layout)[plan.in_.deck]["fader"]
    earlier = layout.start_out_bar - 1.0
    assert shared_start_out_bar([plan], out_profile, out_profile, 4, autodj=build_service()) == layout.start_out_bar
    shifted_layout = layout_of(plan, out_profile, out_profile, 4, start_out_bar=earlier)
    shifted_doc = compile_render_plan(
        plan, out_profile, out_profile, 4, out_audio=ANCHOR, in_audio=ANCHOR, start_out_bar=earlier
    )
    shifted = knob_curves(shifted_doc, plan.out.deck, out_profile, shifted_layout)[plan.in_.deck]["fader"]
    first_zero = _rise_start(own)
    shifted_zero = _rise_start(shifted)
    assert shifted_zero - first_zero == pytest.approx(out_profile.bar_s / layout.out_ratio, abs=1e-3), (
        "if an earlier shared start does not move the lines by the bars between then lanes drift from the audio"
    )


def test_knob_breakpoints_use_render_clock_when_outgoing_is_heard_pitched() -> None:
    """[if] the outgoing deck is heard faster than stored [then] lane times divide by out_ratio, [else stop]."""
    out_stored = profile("fixture-out-heard-lane", bpm=120.0, arrangement=ARRANGEMENT)
    out_heard = heard_profile(out_stored, heard_bpm=127.2)
    in_profile = profile("fixture-in-heard-lane", bpm=124.0, key="9A", arrangement=IN_ARRANGEMENT)
    proposals = propose_transition(out_heard, in_profile, out_deck=1, in_deck=2, top=12)["proposals"]
    plan = next(parse_plan(p["plan"]) for p in proposals if p["plan"]["style"] == "crossfade")
    layout = layout_of(plan, out_heard, in_profile, 4)
    assert layout.out_ratio == pytest.approx(127.2 / 120.0)
    document = compile_render_plan(plan, out_heard, in_profile, 4, out_audio=ANCHOR, in_audio=ANCHOR)
    curves = knob_curves(document, plan.out.deck, out_stored, layout)
    fader = curves[plan.in_.deck]["fader"]
    first_zero = _rise_start(fader)
    plain_layout = dataclasses.replace(layout, out_ratio=1.0)
    expected_plain = out_stored.time_of_bar(plan.out.start_bar) - plain_layout.start_out_s
    assert first_zero == pytest.approx(expected_plain / layout.out_ratio, abs=1e-2)


# ----- energy provenance per source profile (#12496, fan-out 17 sections 5a, 5d, 6) ------------------------------
def _energy_profile(stable_id: str, evidence: str, level: int | None = None):  # type: ignore[no-untyped-def]
    """A profile parsed from a served anlz document carrying MIK energy, only the waveform, or neither."""
    anlz = anlz_payload(stable_id, bpm=124.0, arrangement=ARRANGEMENT)
    if evidence == "mik":
        end_s = anlz["beatgrid"]["beats"][-1]["t"] + 1.0
        anlz |= {
            "energy_source": "mik",
            "energy_level": level,
            "energy_segments": [
                {"start_s": 0.0, "end_s": end_s, "level": level, "source": "mik", "start_clamped": False}
            ],
        }
    elif evidence == "none":
        anlz["waveform"] = None
    bars = sum(row[1] for row in ARRANGEMENT)
    return profile_from_payloads(anlz, track_row(stable_id, bpm=124.0, bars=bars))


@pytest.mark.requirement("PERFORMANCE-SCREEN-14653")
def test_both_mik_profiles_state_mik_and_give_the_cross_track_step() -> None:
    """[if] both track profiles have MIK energy [then] both say mik and the lanes give the MIK step, [else stop]."""
    energy = lane_energy(_energy_profile("out-mik", "mik", 6), _energy_profile("in-mik", "mik", 7))
    assert (energy.out.source, energy.in_.source) == ("mik", "mik")
    assert (energy.out.mik_level, energy.in_.mik_level, energy.cross_track_step) == (6, 7, 1)
    assert "comparable" in energy.note


@pytest.mark.requirement("PERFORMANCE-SCREEN-14653")
def test_one_relative_profile_states_relative_and_never_compares_across_tracks() -> None:
    """[if] one profile has only relative energy [then] it says relative and no cross-track step is given, [else stop]."""
    energy = lane_energy(_energy_profile("out-mik", "mik", 6), _energy_profile("in-rel", "relative"))
    assert (energy.out.source, energy.in_.source) == ("mik", "relative")
    assert energy.in_.mik_level is None
    assert energy.cross_track_step is None, "if relative energy yields a step then it overstates comparability"
    assert "not compared" in energy.note and "in-rel has relative energy only" in energy.note


@pytest.mark.requirement("PERFORMANCE-SCREEN-14653")
def test_profiles_without_energy_evidence_state_none_and_no_step() -> None:
    """[if] neither profile has energy evidence [then] both say none and no cross-track step is given, [else stop]."""
    energy = lane_energy(_energy_profile("out-bare", "none"), _energy_profile("in-bare", "none"))
    assert (energy.out.source, energy.in_.source) == ("none", "none")
    assert energy.cross_track_step is None
    assert "out-bare has no energy evidence" in energy.note and "in-bare has no energy evidence" in energy.note
