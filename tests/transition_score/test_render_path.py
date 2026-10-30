"""The render path: TransitionPlan -> plan.rs JSON -> odj-audio render -> scored candidates (TRYER-02, TRYER-03).

The integration tests drive the real renderer on invented tracks (synthetic
audio written as WAV, profiles from the AutoDJ fixtures); they skip with a
named reason when no odj-audio build exists, unless the job that just built it
sets MDT_REQUIRE_AUDIO_ENGINE_BUILD=1, where a missing engine fails.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from apps.autodj.plan import parse_plan
from apps.autodj.propose import propose_transition
from apps.autodj.render_plan import RenderPlanError, compile_render_plan, layout_of
from apps.shared.odj_audio_binary import OdjAudioUnavailable
from apps.transition_score.contract import SCORER_VERSION
from apps.transition_score.scorer import score_render
from apps.transition_score.tryer import (
    odj_audio_binary,
    read_render,
    render_context,
    render_plan_json,
    rendered_steps,
    try_plans,
    worker_count,
    write_pcm16_set,
)
from tests.autodj.fixtures import CHORUS, INTRO, OUTRO, profile
from tests.transition_score.synth import track_audio

ARRANGEMENT = ((INTRO, 16, 0.4, 0.3, False), (CHORUS, 16, 1.0, 1.0, True), (OUTRO, 16, 0.4, 0.8, False))
OUT_BPM, IN_BPM = 124.0, 126.0


def _profiles():  # type: ignore[no-untyped-def]
    return (
        profile("fixture-out", bpm=OUT_BPM, arrangement=ARRANGEMENT),
        profile("fixture-in", bpm=IN_BPM, key="9A", arrangement=ARRANGEMENT),
    )


def _plans():  # type: ignore[no-untyped-def]
    out_profile, in_profile = _profiles()
    proposed = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, top=6)
    return [parse_plan(p["plan"]) for p in proposed["proposals"]]


#: Native absolute stand-ins: "/a.wav" has no drive on Windows, so it is not absolute there.
A_WAV = Path(Path.cwd().anchor) / "a.wav"
B_WAV = Path(Path.cwd().anchor) / "b.wav"


# ----- compile_render_plan (pure) ---------------------------------------------------


def test_sync_to_out_compiles_to_an_explicit_tempo_never_beat_sync() -> None:
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    doc = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    commands = [e["cmd"] for e in doc["events"] if "cmd" in e]
    assert not any(c["type"] == "beat_sync" for c in commands), (
        "if beat_sync reaches plan.rs then the render is refused"
    )
    tempo = [c for c in commands if c["type"] == "tempo" and c["deck"] == 2]
    assert tempo and abs(tempo[0]["ratio"] - OUT_BPM / IN_BPM) < 1e-9


def test_a_synced_plan_with_an_authored_incoming_tempo_move_is_refused() -> None:
    """[if] a sync_to_out plan moves the incoming deck's tempo [then] the render is refused, because live
    execution moves the outgoing leader instead (compile's _leader_led_events), else stop."""
    import dataclasses

    from apps.autodj.plan import PlanEvent

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    assert plan.tempo_mode == "sync_to_out"
    moved = dataclasses.replace(plan, events=(*plan.events, PlanEvent(2.0, plan.in_.deck, "ramp", "tempo", 1.02, 4.0)))
    with pytest.raises(RenderPlanError, match="leader"):
        compile_render_plan(moved, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)


def test_incoming_strip_sets_are_hoisted_to_render_zero_like_live_prepare() -> None:
    """[if] live compile hoists an incoming set into prepare [then] the render applies it at render time zero,
    before the outgoing context, never on the incoming play's frame, else stop (Codex/Sol on #7597)."""
    from apps.autodj.plan import hoistable

    out_profile, in_profile = _profiles()
    for plan in _plans():
        doc = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
        hoisted = {(e.name, e.value) for e in plan.events if hoistable(e, plan)}
        at_zero = {
            (row["cmd"]["type"], row["cmd"].get("value"))
            for row in doc["events"]
            if "cmd" in row and row["at"] == {"ms": 0} and row["cmd"].get("deck") == plan.in_.deck
        }
        assert hoisted and all(any(t == name.split("_")[0] for t, _ in at_zero) for name, _ in hoisted), (
            plan.style,
            hoisted,
            at_zero,
        )


def test_both_decks_render_with_master_tempo_on_like_live_decks() -> None:
    """Codex P1 on #7597: Rust decks default Master Tempo off, live decks on (AGENTS.md); the render
    must set it explicitly before any tempo command, or the audition shifts pitch the live mix keeps."""
    out_profile, in_profile = _profiles()
    doc = compile_render_plan(_plans()[0], out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    commands = [e["cmd"] for e in doc["events"] if "cmd" in e]
    first_tempo = next(i for i, c in enumerate(commands) if c["type"] == "tempo")
    on = {c["deck"]: i for i, c in enumerate(commands) if c["type"] == "master_tempo" and c["enabled"] is True}
    assert set(on) == {1, 2} and max(on.values()) < first_tempo


def test_render_starts_context_bars_before_the_plan_and_ends_on_the_incoming_deck() -> None:
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    layout = layout_of(plan, out_profile, in_profile, 4)
    # The context counts back from the plan's earliest event, which may sit before the out start bar
    # (a pre-roll fader ramp at bar -1 on the base branch), never from the start bar alone.
    first_event = min(0.0, *(e.at_bar for e in plan.events))
    assert layout.start_out_bar == plan.out.start_bar + first_event - 4
    doc = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    assert doc["end"] == {"deck": 2, "bar": plan.in_.start_bar + plan.bars + 4}
    plays = [e for e in doc["events"] if e.get("cmd", {}).get("type") == "play" and e["cmd"]["deck"] == 2]
    assert plays and plays[0]["at"] == {"deck": 1, "bar": plan.out.start_bar}, (
        "if the incoming play is not on its bar then it lands off the downbeat"
    )


def test_compile_is_deterministic() -> None:
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    docs = [compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV) for _ in range(2)]
    assert json.dumps(docs[0], sort_keys=True) == json.dumps(docs[1], sort_keys=True)


def test_too_little_track_before_the_plan_is_refused_not_clamped() -> None:
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    with pytest.raises(RenderPlanError, match="context"):
        layout_of(plan, out_profile, in_profile, plan.out.start_bar + 5)


def test_workers_leave_two_cores() -> None:
    assert worker_count(64) == max(1, (os.cpu_count() or 1) - 2)
    assert worker_count(1) == 1


# ----- the real renderer -------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Path:
    try:
        return odj_audio_binary()
    except OdjAudioUnavailable as error:
        if os.environ.get("MDT_REQUIRE_AUDIO_ENGINE_BUILD") == "1":
            raise
        # Raised, not called: the gate's isolated mypy has no pytest stubs, so pytest.skip() reads as returning.
        raise pytest.skip.Exception(
            f"UNAVAILABLE: no odj-audio build ({error}); the render path is unmeasured here, not passed"
        ) from error


@pytest.fixture(scope="module")
def tracks(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("tracks")
    out_path, in_path = root / "out.wav", root / "in.wav"
    sf.write(out_path, track_audio(OUT_BPM, 48, 50.0, 41.2, 1), 48000, subtype="PCM_16")
    sf.write(in_path, track_audio(IN_BPM, 48, 62.0, 55.0, 2), 48000, subtype="PCM_16")
    return out_path, in_path


def _assert_ranked_measured_first(candidates: Sequence[Any]) -> None:
    groups = [c.rank_group for c in candidates]
    assert groups == sorted(groups, key=lambda g: g != "measured"), "measured alignment ranks first (round 8)"
    for group in set(groups):
        scores = [c.score for c in candidates if c.rank_group == group and c.score is not None]
        assert scores == sorted(scores, reverse=True)


def test_try_plans_renders_scores_and_ranks_every_candidate(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path
) -> None:
    out_profile, in_profile = _profiles()
    plans = _plans()
    candidates = try_plans(plans, out_profile, in_profile, tracks, context_bars=4, binary=engine)
    assert len(candidates) == len(plans)
    assert all(c.scorer_version == SCORER_VERSION and c.measured.n == 7 for c in candidates)
    assert all("vocal_overlap" in c.unknown for c in candidates), (
        "if vocal overlap reads a number then it was invented: no stems render"
    )
    _assert_ranked_measured_first(candidates)


def test_same_plan_rendered_twice_is_bit_identical_with_identical_metrics(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path
) -> None:
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    doc = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=tracks[0], in_audio=tracks[1])
    layout = layout_of(plan, out_profile, in_profile, 4)
    results = []
    for run in ("one", "two"):
        render = render_plan_json(doc, tmp_path / run, engine)
        mix, out_deck, in_deck = (
            read_render(p, render.summary) for p in (render.mix_path, render.deck_path(1), render.deck_path(2))
        )
        scored = score_render(mix, out_deck, in_deck, render_context(plan, out_profile, layout, render.summary))
        results.append(
            (
                render.summary["sha256"],
                [d["sha256"] for d in render.summary["deck_outputs"]],
                scored.metrics,
                scored.score,
            )
        )
        assert np.isfinite(mix).all()
    assert results[0] == results[1]


def test_bench_table_lists_every_candidate_with_its_veto_and_measured_count() -> None:
    from apps.transition_score.bench import table
    from apps.transition_score.contract import Candidate, Measured, TryResult, Veto

    def candidate(cid: str, score: float | None, vetoes: tuple[Veto, ...]) -> Candidate:
        return Candidate(
            cid,
            {},
            "0" * 64,
            "bass_swap_blend",
            16,
            {"beat_offset_ms_p95": 4.2},
            {},
            vetoes,
            Measured(6, 7),
            {},
            score,
            SCORER_VERSION,
            300.0,
            900.0,
        )

    text = table(TryResult("a", "b", (candidate("c1", 0.8, ()), candidate("c2", 0.0, (Veto("bar_lag", 1.0, 0.0),)))))
    print(text)
    assert "c1" in text and "c2" in text and "bar_lag" in text and "6/7" in text and "4.2" in text


def test_bench_without_an_engine_exits_nonzero_naming_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from apps.transition_score import bench

    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(tmp_path / "absent.lock"))
    code = bench.main(["--pair", "a", "b", "--data-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert f"[ERROR] EngineNotRunning: no engine lock file at {tmp_path / 'absent.lock'}" in err, err


# ----- review round 1 (PR #7597): clipping, seams ---------------


def test_pcm16_rewrite_keeps_overs_measurable(tmp_path: Path) -> None:
    """A render past full scale is scaled, not clipped: read back, its true peak is still over 0 dBTP."""
    from apps.transition_score.signals import true_peak_dbtp

    t = np.arange(48000) / 48000
    loud = (1.6 * np.sin(2 * np.pi * 997 * t)).astype(np.float32)
    paths = [tmp_path / "mix.wav", tmp_path / "deck1.wav"]
    for path, x in zip(paths, (loud, loud / 2), strict=True):
        sf.write(path, np.stack([x, x], axis=1), 48000, subtype="FLOAT")
    gain_db = write_pcm16_set(paths)
    assert sf.info(paths[0]).subtype == "PCM_16"
    summary = {"pcm16_gain_db": gain_db}
    mix, deck = read_render(paths[0], summary), read_render(paths[1], summary)
    assert true_peak_dbtp(mix) > 3.5, "if the 16-bit rewrite clips then the true-peak veto cannot see the overs"
    assert abs(float(np.abs(deck).max()) - 0.8) < 0.01, "if decks get a different gain then mix and decks disagree"


@pytest.mark.skipif(os.name != "posix", reason="the failing renderer stand-in is a POSIX shebang script")
def test_a_failed_render_raises_naming_the_renderer_error(tmp_path: Path) -> None:
    from apps.transition_score.tryer import TryError

    failing = tmp_path / "fail.sh"
    failing.write_text("#!/bin/sh\necho broken >&2\nexit 3\n", encoding="utf-8", newline="\n")
    failing.chmod(0o755)
    doc = {"events": [], "end": {"ms": 1}}
    with pytest.raises(TryError, match="broken"):
        render_plan_json(doc, tmp_path / "render", failing)


def test_render_context_marks_the_incoming_start_as_a_seam() -> None:
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    layout = layout_of(plan, out_profile, in_profile, 4)
    summary = {
        "tempo": [{"deck": 1, "tempo": 1.0}, {"deck": 2, "tempo": 0.99}],
        "sample_rate": 48000,
        "frames": 2_880_000,
        "master_tempo": False,
        "pcm16_gain_db": 0.0,
    }
    ctx = render_context(plan, out_profile, layout, summary)
    play_bars = [e.at_bar for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck]
    expected = out_profile.time_of_bar(plan.out.start_bar + play_bars[0]) - layout.start_out_s
    assert any(abs(s - expected) < 1e-9 for s in ctx.seams_s), (
        "if the deck start is not a seam then its click is never looked for"
    )


@pytest.mark.parametrize(
    ("stated", "parsed"), [("wind down into the break", "wind_down"), ("smooth", "smooth"), (None, None)]
)
def test_render_context_carries_the_plans_parsed_intent_and_never_assumes_one(
    stated: str | None, parsed: str | None
) -> None:
    """[if] a plan states an intent [then] the scorer sees it parsed as the proposer parses it; [if] it states none
    [then] the context says None, never a default that could exempt it from the loudness-loss veto (round 14)."""
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = dataclasses.replace(_plans()[0], intent=stated)
    layout = layout_of(plan, out_profile, in_profile, 4)
    summary = {
        "tempo": [{"deck": 1, "tempo": 1.0}, {"deck": 2, "tempo": 0.99}],
        "sample_rate": 48000,
        "frames": 2_880_000,
        "master_tempo": False,
        "pcm16_gain_db": 0.0,
    }
    ctx = render_context(plan, out_profile, layout, summary)
    assert ctx.intent == parsed
    assert ctx.shape is None, "a plan with no shape (main's plans) must reach the scorer as None, never lull_then_drop"


def test_outgoing_natural_is_the_outgoing_source_on_the_render_clock(tmp_path: Path) -> None:
    """[if] the outgoing file is 44.1 kHz [then] its natural audio comes back at the render rate, from the render's
    start, padded with silence past the file's end, [else stop] (round 14: the loss reference)."""
    import soundfile as sf

    from apps.transition_score.tryer import outgoing_natural

    t = np.arange(44100 * 3) / 44100
    tone = (0.5 * np.sin(2 * np.pi * 440 * t) * (t >= 1.0)).astype(np.float32)  # silent first second, then a tone
    path = tmp_path / "out.wav"
    sf.write(path, np.stack([tone, tone], axis=1), 44100)
    natural = outgoing_natural(path, 1.0, 48000 * 3, 48000)
    assert natural.shape == (48000 * 3, 2)
    assert abs(float(np.abs(natural[1000:47000]).max()) - 0.5) < 0.02, "the render starts where the tone does"
    assert float(np.abs(natural[48000 * 2 + 1000 :]).max()) == 0.0, "past the file's end is silence"


def test_an_off_grid_play_seam_is_scored_where_the_render_starts_it() -> None:
    """[if] a play is authored between downbeats [then] its seam is at the quantized launch the render uses, never
    at the authored bar, [else stop] (Sol P1 on #7597)."""
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    play = next(e for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck)
    late = dataclasses.replace(play, at_bar=play.at_bar + 0.5)
    shifted = dataclasses.replace(plan, events=tuple(late if e is play else e for e in plan.events))
    layout = layout_of(shifted, out_profile, in_profile, 4)
    _, seams = rendered_steps(shifted, out_profile, layout)

    def at(bar: float) -> float:
        return out_profile.time_of_bar(plan.out.start_bar + bar) - layout.start_out_s

    assert any(abs(s - at(play.at_bar + 1)) < 1e-9 for s in seams), seams  # the quantized downbeat
    assert not any(abs(s - at(late.at_bar)) < 1e-9 for s in seams), seams  # never the authored bar


# ----- the engine boots without the analysis extra ------------------------------------------------

_WITHOUT_AUDIO_STACK = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("soundfile", "scipy"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
import apps.transition_score as ts
import apps.transition_score.tryer as tryer
import apps.transition_score.bench
assert "soundfile" not in sys.modules and "scipy" not in sys.modules, sorted(m for m in sys.modules if m.startswith(("scipy", "soundfile")))
try:
    tryer.try_plans([object()], None, None, (None, None), context_bars=4, )
except tryer.TryError as error:
    print("TRYERROR", error)
"""


def test_package_imports_without_soundfile_or_scipy_and_a_try_names_the_extra() -> None:
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", _WITHOUT_AUDIO_STACK],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "TRYERROR the try-er needs the 'analysis' extra" in done.stdout, done.stdout


def test_a_control_event_with_no_value_is_refused_not_rendered_as_zero() -> None:
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    index = next(i for i, e in enumerate(plan.events) if e.kind in ("set", "ramp"))
    for kind in ("set", "ramp"):
        broken_event = dataclasses.replace(plan.events[index], kind=kind, value=None, over_bars=4.0)
        broken = dataclasses.replace(plan, events=(*plan.events[:index], broken_event, *plan.events[index + 1 :]))
        with pytest.raises(RenderPlanError, match="has no numeric value"):
            compile_render_plan(broken, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)


# ----- round 3 (policy): the render plays the incoming deck at the plan's loudness match -----------------


def test_in_trim_db_is_the_incoming_trim_at_time_zero_with_the_page_mapping() -> None:
    import dataclasses

    from apps.autodj.compile import compile_plan
    from apps.autodj.profile import trim_knob

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    plan = dataclasses.replace(
        plan, in_=dataclasses.replace(plan.in_, trim_db=-1.2, trim_reason="match -8.7 to -9.9 LUFS: -1.2 dB")
    )
    doc = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    trims = [e for e in doc["events"] if e.get("cmd", {}).get("type") == "trim" and e["cmd"]["deck"] == 2]
    assert trims == [{"at": {"ms": 0}, "cmd": {"type": "trim", "deck": 2, "value": trim_knob(-1.2)}}]
    page = compile_plan(plan, in_profile, plan_id="p")["plan"]["prepare"]
    assert {"type": "trim", "deck": 2, "value": trim_knob(-1.2)} in page, (
        "if render and page map the trim differently they play different levels"
    )
    first_play = next(i for i, e in enumerate(doc["events"]) if e.get("cmd", {}).get("type") == "play")
    assert doc["events"].index(trims[0]) < first_play


def test_no_trim_db_sends_no_trim_and_an_authored_trim_beside_it_is_refused() -> None:
    import dataclasses

    from apps.autodj.plan import PlanEvent

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    bare = dataclasses.replace(
        plan, in_=dataclasses.replace(plan.in_, trim_db=None, trim_reason="no LUFS for fixture-in")
    )
    doc = compile_render_plan(bare, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    # ADR-0135: only the outgoing's headroom pre-gain; no incoming trim without trim_db.
    assert not [e for e in doc["events"] if e.get("cmd", {}).get("type") == "trim" and e["cmd"]["deck"] == 2]
    both = dataclasses.replace(
        plan,
        in_=dataclasses.replace(plan.in_, trim_db=-1.0),
        events=(*plan.events, PlanEvent(at_bar=1.0, deck=2, kind="set", name="trim", value=0.4)),
    )
    with pytest.raises(RenderPlanError, match="keep one"):
        compile_render_plan(both, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)


# ----- review round 4: telemetry is required, never defaulted ---


_TELEMETRY = {
    "tempo": [{"deck": 1, "tempo": 1.0}, {"deck": 2, "tempo": 0.99}],
    "sample_rate": 48000,
    "frames": 2_880_000,
    "master_tempo": False,
    "pcm16_gain_db": 0.0,
}


@pytest.mark.parametrize("key", sorted(_TELEMETRY))
def test_every_telemetry_field_the_scorer_reads_is_required(key: str) -> None:
    from apps.transition_score.tryer import TryError, parse_telemetry

    assert parse_telemetry(_TELEMETRY, (1, 2)).tempo == ((1, 1.0), (2, 0.99))
    with pytest.raises(TryError, match=key):
        parse_telemetry({k: v for k, v in _TELEMETRY.items() if k != key}, (1, 2))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tempo": []}, "no tempo telemetry for deck"),
        ({"tempo": [{"deck": 1, "tempo": 1.0}]}, "no tempo telemetry for deck"),
        ({"tempo": [{"deck": 1, "tempo": "1"}, {"deck": 2, "tempo": 1.0}]}, "not a float"),
        ({"master_tempo": 0}, "not a bool"),
        ({"sample_rate": 48000.0}, "not a int"),
        ({"pcm16_gain_db": float("nan")}, "not a float"),
    ],
)
def test_malformed_telemetry_is_an_error_never_a_default(change: dict, message: str) -> None:
    from apps.transition_score.tryer import TryError, parse_telemetry

    with pytest.raises(TryError, match=message):
        parse_telemetry({**_TELEMETRY, **change}, (1, 2))


# ----- parity: every control a plan can carry is applied by the render, or refused ---------------------------


def _band_db(x: np.ndarray, lo: float, hi: float) -> float:
    spectrum = np.abs(np.fft.rfft(x[:, 0].astype(np.float64))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / 48000)
    return 10 * np.log10(spectrum[(freqs >= lo) & (freqs < hi)].sum() + 1e-20)


@pytest.fixture(scope="module")
def broadband(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The fixture tracks plus a noise bed, so every band a control acts on has energy to lose."""
    root = tmp_path_factory.mktemp("broadband")
    rng = np.random.default_rng(7)
    paths = []
    for name, bpm, kick, bass in (("out", OUT_BPM, 50.0, 41.2), ("in", IN_BPM, 62.0, 55.0)):
        audio = track_audio(bpm, 48, kick, bass, 3)
        audio = audio + 0.05 * rng.standard_normal(audio.shape).astype(np.float32)
        sf.write(root / f"{name}.wav", audio, 48000, subtype="PCM_16")
        paths.append(root / f"{name}.wav")
    return paths[0], paths[1]


# control, value, band (Hz) whose energy must fall, by at least (dB)
CONTROL_EFFECTS = [
    ("fader", 0.25, (20.0, 20000.0), 6.0),
    ("trim", 0.15, (20.0, 20000.0), 6.0),
    ("filter", 0.15, (2000.0, 20000.0), 12.0),  # below center: lowpass (mixer.rs filter_params)
    ("filter", 0.85, (20.0, 200.0), 12.0),  # above center: highpass
    ("eq_low", 0.0, (20.0, 120.0), 12.0),
    ("eq_mid", 0.0, (700.0, 1500.0), 6.0),
    ("eq_high", 0.0, (6000.0, 20000.0), 12.0),
]


@pytest.mark.parametrize(("control", "value", "band", "drop_db"), CONTROL_EFFECTS)
def test_every_rendered_control_changes_the_render(
    control: str,
    value: float,
    band: tuple[float, float],
    drop_db: float,
    engine: Path,
    broadband: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    import dataclasses

    from apps.autodj.plan import PlanEvent

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    with_event = dataclasses.replace(
        plan, events=(*plan.events, PlanEvent(at_bar=-3.0, deck=1, kind="set", name=control, value=value))
    )
    levels = []
    for run, variant in enumerate((plan, with_event)):
        start_bar = layout_of(with_event, out_profile, in_profile, 4).start_out_bar
        doc = compile_render_plan(
            variant, out_profile, in_profile, 4, out_audio=broadband[0], in_audio=broadband[1], start_out_bar=start_bar
        )
        render = render_plan_json(doc, tmp_path / f"render{run}", engine)
        deck = read_render(render.deck_path(1), render.summary)
        # The event fires at plan bar -3; the render starts context_bars before the earliest event, so it
        # fires at render bar 4. Bar 5 is after it and still before anything the plan itself does.
        start = layout_of(with_event, out_profile, in_profile, 4).start_out_bar
        fires_at = plan.out.start_bar - 3.0 - start
        bar = round(out_profile.bar_s * 48000)
        lo = round((fires_at + 1) * bar)
        levels.append(_band_db(deck[lo : lo + bar], *band))
    print(control, value, band, levels)
    assert levels[0] - levels[1] > drop_db, (
        f"if {control}={value} leaves {band} Hz within {drop_db} dB then the render ignores it"
    )


def test_tempo_control_reaches_the_renderer(engine: Path, broadband: tuple[Path, Path], tmp_path: Path) -> None:
    import dataclasses

    from apps.autodj.plan import PlanEvent

    out_profile, _ = _profiles()
    plan = _plans()[0]
    # The outgoing deck: an incoming tempo move is refused (the render end assumes its starting rate).
    moved = dataclasses.replace(
        plan,
        tempo_mode="none",
        events=(*plan.events, PlanEvent(at_bar=2.0, deck=1, kind="set", name="tempo", value=1.03)),
    )
    # Same BPM on both decks: an unsynced plan across tempos is refused (layout_of).
    doc = compile_render_plan(moved, out_profile, out_profile, 4, out_audio=broadband[0], in_audio=broadband[1])
    render = render_plan_json(doc, tmp_path / "render", engine)
    assert any(p["deck"] == 1 and abs(p["tempo"] - 1.03) < 1e-9 for p in render.summary["tempo"])


def test_every_plan_control_is_either_rendered_or_refused() -> None:
    import dataclasses

    from apps.autodj.plan import CONTROLS, PlanEvent
    from apps.autodj.render_plan import RENDERED_CONTROLS

    assert {c for c, *_ in CONTROL_EFFECTS} | {"tempo"} == set(RENDERED_CONTROLS), (
        "every rendered control has a measured test"
    )
    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    for control in sorted(set(CONTROLS) - set(RENDERED_CONTROLS)):
        bad = dataclasses.replace(
            plan, events=(*plan.events, PlanEvent(at_bar=1.0, deck=None, kind="set", name=control, value=0.5))
        )
        with pytest.raises(RenderPlanError, match="cannot be rendered offline"):
            compile_render_plan(bad, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)


# ----- round 8 (policy): measured alignment ranks first; "Use best" picks only from it ------------------


def _cand(cid: str, score: float | None, alignment_unknown: bool, vetoes: tuple = ()):  # type: ignore[no-untyped-def]
    from apps.transition_score.contract import Candidate, Measured

    unknown = {"alignment": "too few beats timed"} if alignment_unknown else {}
    return Candidate(
        cid,
        {},
        "0" * 64,
        "long_blend",
        16,
        {},
        {},
        vetoes,
        Measured(4 if alignment_unknown else 5, 7),
        unknown,
        score,
        SCORER_VERSION,
        1.0,
        1.0,
    )


def test_a_measured_low_m_candidate_ranks_above_an_unknown_high_m_one() -> None:
    from apps.transition_score.contract import best_candidate, rank_key

    low_measured, high_unknown = _cand("c1", 0.05, False), _cand("c2", 0.9, True)
    ranked = sorted([high_unknown, low_measured], key=rank_key)
    assert [c.id for c in ranked] == ["c1", "c2"]
    assert [c.to_json()["rank_group"] for c in ranked] == ["measured", "alignment unknown"]
    best, why = best_candidate(ranked)
    assert best is low_measured and why == ""


def test_within_a_group_m_orders_and_unscored_comes_last() -> None:
    from apps.transition_score.contract import rank_key

    pool = [_cand("u", None, False), _cand("a", 0.2, False), _cand("b", 0.6, False), _cand("x", 0.99, True)]
    assert [c.id for c in sorted(pool, key=rank_key)] == ["b", "a", "u", "x"]


def test_all_unknown_alignment_yields_no_best_with_the_reason() -> None:
    from apps.transition_score.contract import best_candidate

    best, why = best_candidate([_cand("c1", 0.9, True), _cand("c2", 0.4, True)])
    assert best is None and "alignment is unknown on all 2 candidates" in why


def test_measured_but_all_vetoed_yields_no_best_and_never_falls_back_to_unknown() -> None:
    from apps.transition_score.contract import Veto, best_candidate

    vetoed = _cand("c1", 0.0, False, (Veto("true_peak", 0.5, -1.0),))
    best, why = best_candidate([vetoed, _cand("c2", 0.9, True)])
    assert best is None and "vetoed or unscored" in why


def test_engine_profiles_read_the_same_payloads_as_opendj_dj_without_importing_the_cli() -> None:
    """[if] the try-er reads a profile [then] it uses the engine's track and anlz payloads at the CLI's
    point count, from inside the domain layer (import-linter webui-is-the-top-layer), else stop."""
    import httpx

    from apps.opendj_cli.dj_service import PROFILE_POINTS as CLI_POINTS
    from apps.shared.engine_origin import EngineOrigin
    from apps.transition_score import tryer

    assert tryer.PROFILE_POINTS == CLI_POINTS
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, text="no such track")

    reader = tryer.EngineProfiles(
        EngineOrigin(host="127.0.0.1", port=1, lock_path=Path("/x.lock"), pid=None, role=None, boot_id=None),
        transport=httpx.MockTransport(answer),
    )
    with pytest.raises(tryer.TryError, match="404"):
        reader.profile("a b")
    assert seen == ["http://127.0.0.1:1/api/v1/tracks/a%20b"]


def test_one_unrenderable_proposal_does_not_sink_the_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] some proposals cannot be rendered [then] the rest are still tried and the refusals are noted;
    only when none can be rendered does the pair fail, else stop."""
    from apps.transition_score import tryer

    plans = _plans()
    out_profile, in_profile = _profiles()
    real_layout = tryer.layout_of

    def layout_refusing_the_first(plan: Any, *args: Any, **kwargs: Any) -> Any:
        if plan is plans[0]:
            raise RenderPlanError("not enough incoming context")
        return real_layout(plan, *args, **kwargs)

    monkeypatch.setattr(tryer, "layout_of", layout_refusing_the_first)
    kept, refused = tryer.renderable_plans(plans, out_profile, in_profile, 4)
    assert kept == plans[1:] and len(refused) == 1 and "context" in refused[0]
    monkeypatch.setattr(tryer, "layout_of", lambda *a, **k: (_ for _ in ()).throw(RenderPlanError("no")))
    with pytest.raises(tryer.TryError, match=f"none of {len(plans)} "):
        tryer.renderable_plans(plans, out_profile, in_profile, 4)


def test_an_unsynced_plan_across_different_bpms_is_refused() -> None:
    """[if] a tempo_mode none plan joins tracks of different BPM [then] the layout is refused, because the
    incoming end would be counted in outgoing bars and land before the overlap, else stop."""
    import dataclasses

    out_profile, in_profile = _profiles()
    assert OUT_BPM != IN_BPM
    plan = dataclasses.replace(_plans()[0], tempo_mode="none")
    with pytest.raises(RenderPlanError, match="BPM"):
        layout_of(plan, out_profile, in_profile, 4)
    layout_of(plan, out_profile, out_profile, 4)


def test_a_relative_audio_path_never_reaches_the_renderer() -> None:
    """[if] a load path is relative [then] compile_render_plan refuses it, because the renderer resolves it
    against the plan's own directory, not the caller's, else stop (Sol P1 on #7597)."""
    out_profile, in_profile = _profiles()
    with pytest.raises(RenderPlanError, match="absolute"):
        compile_render_plan(_plans()[0], out_profile, in_profile, 4, out_audio=Path("a.wav"), in_audio=B_WAV)


def test_try_plans_resolves_audio_to_real_absolute_paths_and_refuses_a_missing_one(
    tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] try_plans gets a relative or symlinked track [then] the render loads the real absolute file that was
    hashed; [if] a track is missing [then] TryError names it before any render, else stop."""
    from apps.transition_score import tryer

    out_profile, in_profile = _profiles()
    link = tmp_path / "link.mp3"
    link.symlink_to(tracks[1])
    monkeypatch.chdir(tracks[0].parent)
    seen: list[tuple[Path, Path]] = []

    class Stop(Exception):
        pass

    def record(index: int, plan: Any, job: Any) -> Any:
        seen.append(job.audio)
        raise Stop

    monkeypatch.setattr(tryer, "_candidate", record)
    with pytest.raises(Stop):
        tryer.try_plans(
            _plans()[:1],
            out_profile,
            in_profile,
            (Path(tracks[0].name), link),
            context_bars=4,
            binary=tracks[0],
        )
    assert seen == [(tracks[0].resolve(), tracks[1].resolve())]
    with pytest.raises(tryer.AudioUnavailable, match="fixture-out"):  # the track, never the path
        tryer.try_plans(
            _plans()[:1],
            out_profile,
            in_profile,
            (tmp_path / "missing.mp3", link),
            context_bars=4,
            binary=tracks[0],
        )


def test_an_unsynced_plan_with_an_incoming_tempo_move_is_refused() -> None:
    """[if] an unsynced plan moves the incoming deck's tempo [then] the layout is refused, because the render end
    is counted at the incoming deck's starting rate and would land before the overlap ends, else stop."""
    import dataclasses

    from apps.autodj.plan import PlanEvent

    out_profile, _ = _profiles()
    plan = dataclasses.replace(_plans()[0], tempo_mode="none")
    moved = dataclasses.replace(plan, events=(*plan.events, PlanEvent(2.0, plan.in_.deck, "set", "tempo", 1.08)))
    with pytest.raises(RenderPlanError, match="tempo on the incoming deck"):
        compile_render_plan(moved, out_profile, out_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    compile_render_plan(plan, out_profile, out_profile, 4, out_audio=A_WAV, in_audio=B_WAV)


def test_one_refused_render_leaves_the_other_candidates_ranked(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] one candidate's render is refused [then] only that candidate is marked refused with its reason and
    ranked last, and the rest are scored and returned; [if] every render is refused [then] the try fails, else stop."""
    from apps.transition_score import tryer

    out_profile, in_profile = _profiles()
    plans = _plans()[:3]
    real = tryer.render_plan_json
    calls = {"n": 0}

    def refuse_first(document: Any, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if document["events"][0]["cmd"]["path"] and calls["n"] == 1:
            raise tryer.TryError("renderer exited 3: broken")
        return real(document, *args, **kwargs)

    monkeypatch.setattr(tryer, "worker_count", lambda jobs: 1)
    monkeypatch.setattr(tryer, "render_plan_json", refuse_first)
    candidates = try_plans(plans, out_profile, in_profile, tracks, context_bars=4, binary=engine)
    assert len(candidates) == 3
    assert candidates[-1].refused == "renderer exited 3: broken" and candidates[-1].rank_group == "refused"
    assert all(c.refused is None and c.score is not None for c in candidates[:-1])
    monkeypatch.setattr(tryer, "render_plan_json", lambda *a, **k: (_ for _ in ()).throw(tryer.TryError("down")))
    with pytest.raises(tryer.TryError, match="every"):
        try_plans(plans, out_profile, in_profile, tracks, context_bars=4, binary=engine)


def test_hoisted_incoming_sets_are_scored_at_render_zero_not_their_nominal_bar() -> None:
    """[if] an incoming set is hoisted to render time zero [then] the scorer gets no step for it at its nominal
    time: its steps match the same plan without that set, else stop (Sol P1 on #7597)."""
    import dataclasses

    from apps.autodj.plan import hoistable
    from apps.transition_score import tryer

    out_profile, in_profile = _profiles()
    checked = 0
    for plan in _plans():
        layout = layout_of(plan, out_profile, in_profile, 4)
        hoisted = [e for e in plan.events if hoistable(e, plan) and e.name in tryer._LEVEL_CONTROLS]
        if not hoisted:
            continue
        without = dataclasses.replace(plan, events=tuple(e for e in plan.events if e not in hoisted))
        assert tryer.rendered_steps(plan, out_profile, layout) == tryer.rendered_steps(without, out_profile, layout)
        checked += 1
    assert checked, "control: some proposal hoists a level set"


def test_a_non_finite_render_is_refused_not_scored(tmp_path: Path) -> None:
    """[if] the renderer writes a NaN or infinite sample [then] the render is refused (TryError, so only that
    candidate is refused), never written to 16-bit as if valid, else stop."""
    import numpy as np
    import soundfile as sf

    from apps.transition_score.tryer import TryError, write_pcm16_set

    for bad in (np.nan, np.inf):
        path = tmp_path / f"{bad}.wav"
        samples = np.zeros((4800, 2), dtype=np.float32)
        samples[10, 0] = bad
        sf.write(path, samples, 48000, subtype="FLOAT")
        with pytest.raises(TryError, match="non-finite"):
            write_pcm16_set([path])


@pytest.mark.skipif(os.name != "posix", reason="the renderer stand-in is a POSIX shebang script")
def test_a_renderer_that_prints_no_summary_is_a_refusal(tmp_path: Path) -> None:
    """[if] the renderer exits 0 without a JSON summary line [then] TryError names it, never an IndexError or a
    JSON error that would sink the whole try, else stop."""
    from apps.transition_score.tryer import TryError

    for output in ("", "not json"):
        silent = tmp_path / f"r{len(output)}.sh"
        silent.write_text(f"#!/bin/sh\necho '{output}'\nexit 0\n", encoding="utf-8", newline="\n")
        silent.chmod(0o755)
        with pytest.raises(TryError, match="summary"):
            render_plan_json({"events": [], "end": {"ms": 1}}, tmp_path / f"render{len(output)}", silent)


@pytest.mark.skipif(os.name != "posix", reason="the renderer stand-in is a POSIX shebang script")
def test_a_renderer_past_its_deadline_is_a_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a render runs past RENDER_TIMEOUT_S [then] TryError names the deadline (so only that candidate is
    refused), never a TimeoutExpired that sinks the whole try, else stop (Codex P2 on #7597)."""
    from apps.transition_score import tryer

    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8", newline="\n")
    slow.chmod(0o755)
    monkeypatch.setattr(tryer, "RENDER_TIMEOUT_S", 0.5)
    with pytest.raises(tryer.TryError, match="deadline"):
        render_plan_json({"events": [], "end": {"ms": 1}}, tmp_path / "render", slow)


# ----- renderer failure surface: every failure refuses one candidate (Sol P1 on #7597) -----------------


def _stand_in(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


#: A stand-in renderer that answers like odj-audio: it reads --plan and prints a summary naming that plan's sha256,
#: then does whatever ``$1``-free body the case adds before exiting 0.
_SUMMARY = 'plan="$3"; work=$(dirname "$plan"); mkdir -p "$work/decks"; sha=$(shasum -a 256 "$plan" | cut -d" " -f1)\n'


@pytest.mark.skipif(os.name != "posix", reason="the renderer stand-ins are POSIX shebang scripts")
@pytest.mark.parametrize(
    ("case", "body", "match"),
    [
        ("killed by a signal", "kill -9 $$\n", "render failed"),
        ("no output files", _SUMMARY + 'echo "{\\"plan_sha256\\": \\"$sha\\"}"\n', "no deck outputs"),
        (
            "unreadable output",
            _SUMMARY
            + 'echo garbage > "$work/mix.wav"; echo garbage > "$work/decks/deck1.wav"; echo "{\\"plan_sha256\\": \\"$sha\\"}"\n',
            "renderer output",
        ),
    ],
)
def test_every_renderer_failure_is_a_refusal(tmp_path: Path, case: str, body: str, match: str) -> None:
    """[if] the renderer crashes, is killed, or exits 0 with missing or unreadable output [then] TryError names
    it, so only that candidate is refused, else stop."""
    from apps.transition_score.tryer import TryError

    stand_in = _stand_in(tmp_path, "r.sh", body)
    with pytest.raises(TryError, match=match):
        render_plan_json({"events": [], "end": {"ms": 1}}, tmp_path / "render", stand_in)


def test_a_renderer_that_cannot_be_spawned_is_a_refusal(tmp_path: Path) -> None:
    """[if] the renderer path cannot be executed (no exec bit, missing file) [then] TryError names it, else stop."""
    from apps.transition_score.tryer import TryError

    not_executable = tmp_path / "odj-audio"
    not_executable.write_bytes(b"\0")
    with pytest.raises(TryError, match="could not start"):
        render_plan_json({"events": [], "end": {"ms": 1}}, tmp_path / "render", not_executable)


def test_a_programming_error_still_propagates(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] our own code raises (a TypeError, not a renderer failure) [then] the try fails loudly rather than
    marking a candidate refused, else stop."""
    from apps.transition_score import tryer

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("a defect in our code")

    monkeypatch.setattr(tryer, "score_render", broken, raising=False)
    monkeypatch.setattr("apps.transition_score.scorer.score_render", broken)
    out_profile, in_profile = _profiles()
    with pytest.raises(TypeError, match="defect"):
        try_plans(_plans()[:1], out_profile, in_profile, tracks, context_bars=4, binary=engine)


# ----- no render cache (D7): every request renders fresh, and its files do not outlive it --------------


def test_each_candidate_renders_16_bit_into_a_temp_dir_removed_after_scoring(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a try renders candidates [then] each render is 16-bit while it is read, lives in its own temporary
    directory, and no render file remains once the try returns, else stop."""
    import tempfile

    from apps.transition_score import tryer

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    seen: list[str] = []
    real = tryer.render_plan_json

    def spy(document: Any, directory: Path, binary: Any = None) -> Any:
        render = real(document, directory, binary)
        seen.append(sf.info(render.mix_path).subtype)
        assert directory.parent.name.startswith(tryer.RENDER_TMP_PREFIX)
        return render

    monkeypatch.setattr(tryer, "render_plan_json", spy)
    out_profile, in_profile = _profiles()
    candidates = try_plans(_plans()[:2], out_profile, in_profile, tracks, context_bars=4, binary=engine)
    assert len(candidates) == 2 and seen == ["PCM_16", "PCM_16"]
    assert list(scratch.iterdir()) == [], "if a render outlives its request then the cache D7 removed is back"


@pytest.mark.parametrize(
    ("field", "write", "summary"),
    [
        ("sample rate", {"samplerate": 44100, "channels": 2}, {"sample_rate": 48000, "channels": 2, "frames": 480}),
        ("channels", {"samplerate": 48000, "channels": 1}, {"sample_rate": 48000, "channels": 2, "frames": 480}),
        # The summary's own count is what the file is checked against, never a reader-side constant.
        ("channels", {"samplerate": 48000, "channels": 2}, {"sample_rate": 48000, "channels": 1, "frames": 480}),
        ("frames", {"samplerate": 48000, "channels": 2}, {"sample_rate": 48000, "channels": 2, "frames": 481}),
    ],
)
def test_a_wav_header_that_disagrees_with_the_summary_is_refused(
    tmp_path: Path, field: str, write: dict, summary: dict
) -> None:
    """[if] a rendered WAV's sample rate, channel count or frame count differs from the renderer's summary
    [then] TryError names the file and field, so that candidate is refused; a matching header passes, else stop."""
    import numpy as np

    from apps.transition_score.tryer import TryError, check_wav_headers

    path = tmp_path / "mix.wav"
    sf.write(path, np.zeros((480, write["channels"]), dtype=np.float32), write["samplerate"], subtype="FLOAT")
    with pytest.raises(TryError, match=f"mix.wav: WAV {field}"):
        check_wav_headers([path], summary)
    good = tmp_path / "good.wav"
    sf.write(good, np.zeros((480, 2), dtype=np.float32), 48000, subtype="FLOAT")
    check_wav_headers([good], {"sample_rate": 48000, "channels": 2, "frames": 480})
    with pytest.raises(TryError, match="channels"):
        check_wav_headers([good], {"sample_rate": 48000, "frames": 480})  # a summary without it is refused


def test_render_plan_json_checks_headers_against_the_summary_it_got(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the renderer's summary disagrees with the WAVs it wrote [then] render_plan_json refuses the render,
    else stop (the per-field cases are above; this pins that the check runs on every render)."""
    from apps.transition_score import tryer

    real = tryer._render_into

    def one_frame_off(*args: Any) -> Any:
        summary, render_ms = real(*args)
        return {**summary, "frames": summary["frames"] + 1}, render_ms

    monkeypatch.setattr(tryer, "_render_into", one_frame_off)
    out_profile, in_profile = _profiles()
    doc = compile_render_plan(_plans()[0], out_profile, in_profile, 4, out_audio=tracks[0], in_audio=tracks[1])
    with pytest.raises(tryer.TryError, match="WAV frames"):
        render_plan_json(doc, tmp_path / "render", engine)


# ----- keep_renders_in: the caller keeps a request's renders (D7 amended) ---------------------------------


def test_renders_are_not_kept_unless_asked(engine: Path, tracks: tuple[Path, Path], tmp_path: Path) -> None:
    """[if] try_plans is called without keep_renders_in [then] every render_path is None, else stop (that nothing
    is left on disk is pinned by test_each_candidate_renders_16_bit_into_a_temp_dir_removed_after_scoring)."""
    out_profile, in_profile = _profiles()
    candidates = try_plans(_plans()[:2], out_profile, in_profile, tracks, context_bars=4, binary=engine)
    assert [c.render_path for c in candidates] == [None, None]


def test_keep_renders_in_leaves_exactly_each_candidates_checked_mix_and_decks(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] keep_renders_in is given [then] it holds exactly <cid>/mix.wav and <cid>/decks/deck1.wav, deck2.wav per
    candidate and nothing else, render_path names the mix, and the copies pass the header check against their
    render's summary, else stop."""
    from apps.transition_score import tryer

    summaries: list[dict] = []
    real = tryer.render_plan_json

    def spy(document: Any, directory: Path, binary: Any = None) -> Any:
        render = real(document, directory, binary)
        summaries.append(render.summary)
        return render

    monkeypatch.setattr(tryer, "worker_count", lambda jobs: 1)
    monkeypatch.setattr(tryer, "render_plan_json", spy)
    keep = tmp_path / "kept"
    out_profile, in_profile = _profiles()
    candidates = try_plans(
        _plans()[:2], out_profile, in_profile, tracks, context_bars=4, binary=engine, keep_renders_in=keep
    )
    expected = sorted(
        f"{cid}/{name}" for cid in ("c1", "c2") for name in ("mix.wav", "decks/deck1.wav", "decks/deck2.wav")
    )
    assert sorted(str(p.relative_to(keep)) for p in keep.rglob("*") if p.is_file()) == expected
    by_id = {c.id: c for c in candidates}
    for index, cid in enumerate(("c1", "c2")):
        assert by_id[cid].render_path == str(keep / cid / "mix.wav")
        files = sorted((keep / cid).rglob("*.wav"))
        assert {sf.info(f).subtype for f in files} == {"PCM_16"}
        tryer.check_wav_headers(files, summaries[index])


def test_an_existing_kept_folder_is_refused_before_any_render(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] <dir>/<cid>/ already exists [then] try_plans raises before rendering and its contents are untouched;
    a kept render is never overwritten, else stop."""
    from apps.transition_score import tryer

    keep = tmp_path / "kept"
    (keep / "c2").mkdir(parents=True)
    (keep / "c2" / "mine.txt").write_text("keep me", encoding="utf-8", newline="\n")
    monkeypatch.setattr(tryer, "render_plan_json", lambda *a, **k: pytest.fail("rendered before refusing"))
    out_profile, in_profile = _profiles()
    with pytest.raises(FileExistsError, match="c2"):
        try_plans(_plans()[:2], out_profile, in_profile, tracks, context_bars=4, binary=engine, keep_renders_in=keep)
    assert sorted(p.name for p in (keep / "c2").iterdir()) == ["mine.txt"]


def _incoming_play_bar(doc: dict[str, Any], in_deck: int) -> float:
    rows = [r for r in doc["events"] if r.get("cmd", {}).get("type") == "play" and r["cmd"]["deck"] == in_deck]
    assert len(rows) == 1 and rows[0]["cmd"]["playing"] is True, rows
    return float(rows[0]["at"]["bar"])


def test_an_off_grid_play_renders_at_the_executors_quantized_downbeat() -> None:
    """[if] a play is authored between downbeats [then] the render starts it on the next downbeat, where the
    quantized live launch does, while an on-grid play is unchanged, [else stop] (Sol P1 on #7597)."""
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    play = next(e for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck)
    assert play.at_bar == int(play.at_bar), "fixture: the proposed play is on a downbeat"
    on_grid = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    assert _incoming_play_bar(on_grid, plan.in_.deck) == plan.out.start_bar + play.at_bar  # control: unchanged
    late = dataclasses.replace(play, at_bar=play.at_bar + 0.5)
    shifted = dataclasses.replace(plan, events=tuple(late if e is play else e for e in plan.events))
    doc = compile_render_plan(shifted, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    assert _incoming_play_bar(doc, plan.in_.deck) == plan.out.start_bar + play.at_bar + 1
    # Overshoot control: only a play is quantized; a stop between downbeats takes effect at its own bar.
    stop = next(e for e in plan.events if e.kind == "do" and e.name == "stop")
    late_stop = dataclasses.replace(stop, at_bar=stop.at_bar - 0.5)
    stopped = dataclasses.replace(plan, events=tuple(late_stop if e is stop else e for e in plan.events))
    doc = compile_render_plan(stopped, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    rows = [r for r in doc["events"] if r.get("cmd", {}).get("type") == "play" and r["cmd"]["playing"] is False]
    assert [r["at"]["bar"] for r in rows] == [plan.out.start_bar + late_stop.at_bar]


def test_the_render_end_counts_only_bars_the_incoming_deck_plays() -> None:
    """[if] the incoming deck launches at bar 8 [then] the render ends 8 incoming bars earlier than a bar-0 launch,
    [else stop] (Codex P2 / Sol P1 on #7597)."""
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    play = next(e for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck)
    assert play.at_bar == 0.0, "fixture: the proposed play launches at bar 0"
    base = layout_of(plan, out_profile, in_profile, 4)
    late = dataclasses.replace(
        plan, events=tuple(dataclasses.replace(e, at_bar=8.0) if e is play else e for e in plan.events)
    )
    shifted = layout_of(late, out_profile, in_profile, 4)
    assert shifted.end_in_bar == base.end_in_bar - 8 * base.in_fold


def test_the_render_deadline_covers_the_whole_render_clock_whatever_the_launch_bar() -> None:
    """[if] the incoming deck launches late [then] max_ms still covers every plan bar on the outgoing clock plus the
    context, the same as a bar-0 launch, [else stop] (Sol P1 / Codex P2 on #7597)."""
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    play = next(e for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck)
    late = dataclasses.replace(
        plan, events=tuple(dataclasses.replace(e, at_bar=14.0) if e is play else e for e in plan.events)
    )
    base = compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    shifted = compile_render_plan(late, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    assert shifted["max_ms"] == base["max_ms"]


def test_a_candidate_refused_after_rendering_keeps_no_folder(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a candidate is refused after its render (bad telemetry) [then] nothing is left under keep_renders_in, so
    a retry into the same folder is not blocked, [else stop] (Sol P1 on #7597)."""
    from apps.transition_score import tryer
    from apps.transition_score.tryer import TryError

    real_context = tryer.render_context
    calls: list[int] = []

    def first_context_bad(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise TryError("telemetry malformed")
        return real_context(*args, **kwargs)

    monkeypatch.setattr(tryer, "worker_count", lambda jobs: 1)
    monkeypatch.setattr(tryer, "render_context", first_context_bad)
    keep = tmp_path / "kept"
    out_profile, in_profile = _profiles()
    candidates = try_plans(
        _plans()[:2], out_profile, in_profile, tracks, context_bars=4, binary=engine, keep_renders_in=keep
    )
    by_id = {c.id: c for c in candidates}
    assert by_id["c1"].refused is not None and not (keep / "c1").exists()
    assert by_id["c2"].refused is None and (keep / "c2" / "mix.wav").is_file()  # control: a scored one is kept


def test_the_render_duration_comes_from_the_checked_frame_count() -> None:
    """[if] the summary's duration_ms disagrees with frames / sample_rate [then] the scorer uses the frames, the
    count the WAV headers were checked against, [else stop] (Sol P1)."""
    from apps.transition_score.tryer import parse_telemetry

    stale = {**_TELEMETRY, "duration_ms": 1.0}
    assert parse_telemetry(stale, (1, 2)).duration_s == 60.0


def test_a_non_numeric_render_target_is_refused_never_coerced() -> None:
    """[if] a set's value is a bool, a string or not finite [then] the render plan is refused, [else stop]."""
    import dataclasses

    out_profile, in_profile = _profiles()
    plan = _plans()[0]
    step = next(e for e in plan.events if e.kind == "set")
    compile_render_plan(plan, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)  # control
    for bad in (True, "0.5", float("nan")):
        broken = dataclasses.replace(
            plan, events=tuple(dataclasses.replace(e, value=bad) if e is step else e for e in plan.events)
        )
        with pytest.raises(RenderPlanError, match="numeric value"):
            compile_render_plan(broken, out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)


def test_a_missing_audio_file_names_the_track_never_the_path(tmp_path: Path) -> None:
    """[if] a resolved audio file is gone [then] AudioUnavailable names the stable id and no path, [else stop]."""
    from apps.transition_score.tryer import AudioUnavailable, _real_audio_paths

    present = tmp_path / "a.wav"
    present.write_bytes(b"x")
    assert _real_audio_paths((present, present), ("o", "i"))[0] == present.resolve()  # control
    with pytest.raises(AudioUnavailable) as refused:
        _real_audio_paths((present, tmp_path / "gone.wav"), ("o", "trk-in"))
    assert refused.value.stable_id == "trk-in" and str(tmp_path) not in str(refused.value)


def test_only_pair_level_refusals_are_pair_not_tryable(
    engine: Path, tracks: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the pair itself cannot be tried [then] PairNotTryable, which a batch may skip; [if] the renderer fails
    for every candidate [then] a plain TryError, which must abort a batch, [else stop] (Sol P1 on #7596)."""
    from apps.transition_score import tryer
    from apps.transition_score.tryer import PairNotTryable, TryError

    out_profile, in_profile = _profiles()
    with pytest.raises(PairNotTryable):
        try_plans([], out_profile, in_profile, tracks, context_bars=4, binary=engine)
    with pytest.raises(PairNotTryable):
        tryer._real_audio_paths((tracks[0], tmp_path / "gone.wav"), ("o", "i"))

    def broken_renderer(*_args: object, **_kwargs: object) -> None:
        raise TryError("odj-audio could not start")

    monkeypatch.setattr(tryer, "render_plan_json", broken_renderer)
    with pytest.raises(TryError) as systemic:
        try_plans(_plans()[:2], out_profile, in_profile, tracks, context_bars=4, binary=engine)
    assert not isinstance(systemic.value, PairNotTryable)


@pytest.mark.requirement("AGENT-47")
def test_the_outgoing_deck_loads_at_unity_unless_the_headroom_is_opted_into() -> None:
    """[if] any plan is compiled for render [then] no outgoing TRIM is sent, [else stop].

    ADR-0135, superseded in part by round 22: the pre-gain is opt-in."""
    out_profile, in_profile = _profiles()
    doc = compile_render_plan(_plans()[0], out_profile, in_profile, 4, out_audio=A_WAV, in_audio=B_WAV)
    events = doc["events"]
    trims = [e for e in events if e.get("cmd", {}).get("type") == "trim" and e["cmd"]["deck"] == 1]
    assert trims == [], "if the render sets an outgoing trim by default then the headroom opt-in leaked back"
