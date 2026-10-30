"""Two-deck lanes for a try-er render (TRYER-16): waveform peaks and knob curves on the render clock.

Pure: profiles from the AutoDJ fixtures, plans from the real proposer, deck renders written as 16-bit WAV here.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from apps.autodj.plan import parse_plan
from apps.autodj.propose import propose_transition
from apps.autodj.render_plan import compile_render_plan, layout_of
from apps.transition_score.lanes import (
    POWER_ON,
    LaneError,
    beat_grid,
    build_lanes,
    deck_peaks,
    knob_curves,
    playing_spans,
)
from tests.autodj.fixtures import CHORUS, INTRO, OUTRO, profile

ARRANGEMENT = ((INTRO, 16, 0.4, 0.3, False), (CHORUS, 16, 1.0, 1.0, True), (OUTRO, 16, 0.4, 0.8, False))
ANCHOR = Path(Path.cwd().anchor)


def _case(style: str):  # type: ignore[no-untyped-def]
    out_profile = profile("fixture-out", bpm=124.0, arrangement=ARRANGEMENT)
    in_profile = profile("fixture-in", bpm=124.0, key="9A", arrangement=ARRANGEMENT)
    proposals = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, top=12)["proposals"]
    plan = next(parse_plan(p["plan"]) for p in proposals if p["plan"]["style"] == style)
    layout = layout_of(plan, out_profile, in_profile, 4)
    document = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=ANCHOR, in_audio=ANCHOR)
    return plan, out_profile, layout, document


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
    first_zero = times[values.index(0.0)]
    expected = out_profile.time_of_bar(plan.out.start_bar) - layout.start_out_s
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
    lanes = build_lanes(tmp_path, document, plan.out.deck, out_profile, layout, 50)
    assert [lane.deck for lane in lanes.decks] == [1, 2]
    incoming = lanes.decks[1]
    assert incoming.playing and incoming.playing[0][0] > 0.0, (
        "if the incoming deck plays from t0 then its play command was not read"
    )
    assert lanes.decks[0].playing[0][0] == 0.0


def test_build_lanes_without_deck_renders_fails_loud(tmp_path: Path) -> None:
    plan, out_profile, layout, document = _case("crossfade")
    with pytest.raises(LaneError):
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
    assert shared_start_out_bar([plan], out_profile, out_profile, 4) == layout.start_out_bar
    shifted_layout = layout_of(plan, out_profile, out_profile, 4, start_out_bar=earlier)
    shifted_doc = compile_render_plan(
        plan, out_profile, out_profile, 4, out_audio=ANCHOR, in_audio=ANCHOR, start_out_bar=earlier
    )
    shifted = knob_curves(shifted_doc, plan.out.deck, out_profile, shifted_layout)[plan.in_.deck]["fader"]
    first_zero = next(t for t, v in own if v == 0.0)
    shifted_zero = next(t for t, v in shifted if v == 0.0)
    assert shifted_zero - first_zero == pytest.approx(out_profile.bar_s, abs=1e-3), (
        "if an earlier shared start does not move the lines by the bars between then lanes drift from the audio"
    )
