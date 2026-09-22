"""Mutation tests: real audio, the real model, real ffmpeg. No synthetic beat times.

The unit suites pin the detector's arithmetic against beat times supplied by
hand. That leaves the question those tests cannot reach: does the WHOLE chain --
audio in, model, peak picker, bar numbering, tempo fit, changepoint detector --
still say the right thing when a tempo change is genuinely present in the
waveform? These tests answer that by BUILDING the audio, MUTATING it with
ffmpeg, and running the shipped runner over both versions.

The lane brief's acceptance lines, and what each of them turned out to be:

  1. A 128 BPM click track reads 128.00 +/- 0.01 BPM. Holds exactly.
  2. A 3 percent tempo step injected at 2:00 produces a marker within 8 bars.
     Holds.
  3. The SAME untouched track produces zero markers. Holds, and without it
     line 2 would be satisfied by a detector that fires on everything.
  4. "Bar 1 on the accented click" DOES NOT HOLD, and the reason is a measured
     property of Beat This! rather than of this repo: on a bare metronome it
     emits 191 downbeats for 427 beats against 107 true accents, and the
     runner's `--verify-postprocessor` control confirms those 191 are byte
     identical to beat_this's own postprocessor. Synthetic click audio is not
     a valid instrument for downbeat accuracy. What is asserted instead is the
     part this lane owns (the numbering chain) plus a pinned density figure so
     the finding cannot rot into a belief. Full reasoning in the two tests.

WHY THESE ARE OPT-IN. They need `uv`, a network fetch of the Beat This!
checkpoint on first run, ffmpeg, and roughly a minute of CPU. That is not a
fast-lane test. They are gated on MDT_BEATGRID_MODEL_TESTS=1 and are RUN BY
HAND per round, with the output pasted into the PR. The gate is an explicit
environment variable rather than a try/except on an import, because a test that
skips itself when something is broken is a check that cannot fail.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import shutil
import struct
import subprocess
import wave

import pytest

from apps.analysis_beatgrid.bar_phase import BAR_BEATS, lock_bar_phase
from apps.analysis_beatgrid.bpm import estimate_bpm
from apps.analysis_beatgrid.tempo_change import MIN_SEGMENT_BARS, detect_tempo_changes
from apps.analysis_beatgrid.tempo_map import fit_tempo_map

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(REPO_ROOT, "apps", "analysis_beatgrid", "beat_this_runner.py")

SAMPLE_RATE = 44100
CLICK_BPM = 128.0
TRACK_S = 200.0            # long enough to hold a change at 2:00 plus 8 bars after
RAMP_AT_S = 120.0
RAMP_FACTOR = 1.03         # the 3 percent of the acceptance line
# The ramp is spread over 8 bars, which is the detector's own MIN_SEGMENT_BARS.
# Shorter and it degenerates towards the step this replaced; much longer and no
# 8-bar window contains enough of the change for any detector to see it, which
# would be a test of the ramp length rather than of the detector.
RAMP_OVER_S = 8 * 4 * (60.0 / CLICK_BPM)

pytestmark = pytest.mark.skipif(
    os.environ.get("MDT_BEATGRID_MODEL_TESTS") != "1",
    reason=(
        "needs uv, ffmpeg, the Beat This! checkpoint and about a minute of CPU; "
        "set MDT_BEATGRID_MODEL_TESTS=1 to run"
    ),
)


# ----- Building audio -----------------------------------------------------


def _steady_beat_times(bpm: float, seconds: float) -> list[float]:
    """Beat onsets for a constant tempo."""
    period = 60.0 / bpm
    return [i * period for i in range(int(seconds / period) + 1) if i * period < seconds]


def _ramped_beat_times(
    bpm: float, seconds: float, ramp_at_s: float, ramp_over_s: float, factor: float
) -> list[float]:
    """Beat onsets for a tempo that RAMPS CONTINUOUSLY rather than stepping.

    NATIVE-03's acceptance line asks for "an injected 3 percent ramp at 2:00",
    and the previous version of this module produced a STEP: it spliced an
    `atempo`-stretched tail onto an untouched head, which is a discontinuity at
    one sample boundary. A changepoint detector can pass that and still fail on
    real drift, where the tempo moves gradually and no single inter-beat
    interval is anomalous (Codex P1 BLOCKING on PR #1514).

    So the ramp is built into the beat GRID itself: tempo holds at `bpm` until
    `ramp_at_s`, rises linearly to `bpm * factor` over `ramp_over_s`, then
    holds. Every click waveform is byte-identical to every other, so only the
    SPACING changes -- no resample, no pitch shift, nothing else for the model
    to react to, which was the property the ffmpeg splice was chosen for and
    this keeps.
    """
    times: list[float] = []
    t = 0.0
    while t < seconds:
        times.append(t)
        if t < ramp_at_s:
            current = bpm
        elif t >= ramp_at_s + ramp_over_s:
            current = bpm * factor
        else:
            progress = (t - ramp_at_s) / ramp_over_s
            current = bpm * (1.0 + (factor - 1.0) * progress)
        t += 60.0 / current
    return times


def _render_clicks(path: str, beat_times: list[float], seconds: float) -> list[float]:
    """Write a 4/4 click track at `beat_times`: bright accent on 1, duller on 2, 3, 4.

    Beat This! is trained on music, not on metronomes, so a bare impulse train
    tracks poorly. Each click is a short decaying tone burst, and the downbeat
    burst is both louder and a fifth higher, which is the cue a downbeat
    tracker is meant to find. Returns the true beat times so a test can assert
    against what was actually written rather than against a recomputation.
    """
    n_samples = int(seconds * SAMPLE_RATE)
    samples = [0.0] * n_samples

    for beat, t0 in enumerate(beat_times):
        is_downbeat = beat % 4 == 0
        frequency = 1800.0 if is_downbeat else 1200.0
        amplitude = 0.9 if is_downbeat else 0.45
        burst = int(0.045 * SAMPLE_RATE)
        start = int(t0 * SAMPLE_RATE)
        for i in range(burst):
            if start + i >= n_samples:
                break
            envelope = math.exp(-i / (0.010 * SAMPLE_RATE))
            samples[start + i] += (
                amplitude * envelope * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE)
            )

    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(
            b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000)) for s in samples)
        )
    return beat_times


def _inject_tempo_step(source: str, target: str, at_s: float, factor: float) -> None:
    """Split at `at_s`, time-stretch the tail by `factor`, and concatenate.

    ffmpeg `atempo` preserves pitch, so the mutation is a tempo change and not
    a resample: a resample would move the click frequencies too and the model
    could plausibly react to that instead of to the tempo.
    """
    head = target + ".head.wav"
    tail = target + ".tail.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source, "-t", str(at_s), head],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source, "-ss", str(at_s),
         "-filter:a", f"atempo={factor}", tail],
        check=True,
    )
    listing = target + ".txt"
    with open(listing, "w", encoding="utf-8") as fh:
        fh.write(f"file '{head}'\nfile '{tail}'\n")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", listing, "-c", "copy", target],
        check=True,
    )


def _run_model(wav_paths: list[str], out_path: str) -> dict:
    subprocess.run(
        ["uv", "run", "--no-project", "--script", RUNNER,
         "--out", out_path, "--device", "cpu", "--measure-rss", "--audio", *wav_paths],
        check=True,
        cwd=REPO_ROOT,
    )
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


# ----- Fixtures -----------------------------------------------------------


@pytest.fixture(scope="module")
def analyzed(tmp_path_factory) -> dict:
    """Build both tracks, run the model once over both, return its output."""
    for tool in ("uv", "ffmpeg"):
        if shutil.which(tool) is None:
            pytest.fail(f"{tool} is required by these tests and is not on PATH")

    workdir = tmp_path_factory.mktemp("beatgrid-mutation")
    steady = str(workdir / "steady.wav")
    stepped = str(workdir / "stepped.wav")
    ramped = str(workdir / "ramped.wav")
    ramped_4pct = str(workdir / "ramped_4pct.wav")

    truth = _render_clicks(steady, _steady_beat_times(CLICK_BPM, TRACK_S), TRACK_S)
    # The step arm keeps the original ffmpeg mutation: an abrupt change is a
    # real and EASIER case, and keeping it separates "cannot detect anything"
    # from "cannot detect a gradual change".
    _inject_tempo_step(steady, stepped, RAMP_AT_S, RAMP_FACTOR)
    ramp_truth = _ramped_beat_times(CLICK_BPM, TRACK_S, RAMP_AT_S, RAMP_OVER_S, RAMP_FACTOR)
    _render_clicks(ramped, ramp_truth, TRACK_S)
    ramp_4pct_truth = _ramped_beat_times(
        CLICK_BPM, TRACK_S, ramp_at_s=90.0, ramp_over_s=30.0, factor=1.04
    )
    _render_clicks(ramped_4pct, ramp_4pct_truth, TRACK_S)

    payload = _run_model([steady, stepped, ramped, ramped_4pct], str(workdir / "beats.json"))
    return {
        "payload": payload,
        "steady": steady,
        "stepped": stepped,
        "ramped": ramped,
        "ramped_4pct": ramped_4pct,
        "truth": truth,
        "ramp_truth": ramp_truth,
        "ramp_4pct_truth": ramp_4pct_truth,
    }


# ----- The acceptance lines -----------------------------------------------


def test_a_128_bpm_click_track_reads_128_00(analyzed):
    """BPM 128.00 +/- 0.01, the rekordbox storage granularity."""
    result = analyzed["payload"]["results"][analyzed["steady"]]
    assert result["error"] is None
    estimate = estimate_bpm(result["beats"])
    assert estimate is not None
    assert estimate.bpm == pytest.approx(128.0, abs=0.01)
    assert estimate.octave_multiple == 1.0


def test_every_beat_is_numbered_consistently_with_the_emitted_downbeats(analyzed):
    """Bar numbering is exactly the emitted downbeats, counting upward between them.

    THIS IS NOT THE BAR-1 ACCURACY TEST, AND THE DIFFERENCE IS A MEASURED
    FINDING, NOT A CLIMBDOWN. The lane brief asked for "bar 1 on the accented
    click". Measured Tue 8 Sep 2026 on the click track this module builds: 427
    beats, of which Beat This! calls 191 downbeats, against 107 true accents.
    It labels roughly every second beat a downbeat.

    `apps/analysis_beatgrid/beat_this_runner.py --verify-postprocessor` on the
    SAME file returns "beats: 427 identical to beat_this" and "downbeats: 191
    identical to beat_this", so that behavior is the MODEL's and not this
    repo's peak picker. Beat This! is trained on music and infers bar structure
    from harmony and phrasing; a loud, high-pitched accent alone does not supply
    it. A synthetic metronome is therefore not a valid instrument for measuring
    downbeat accuracy, and loosening the threshold until it passed would have
    produced a check that could not fail.

    The honest bar-1 measurement is the benchmark's downbeat-agreement column
    against real rekordbox bar-1 beats on real music, which is where round 0
    read 73.0 percent on fixed grids.

    What IS testable here, and what this lane actually owns, is the numbering
    chain: snapping downbeats onto beats and running `assign_bar_phase` over
    the REAL model output must yield a number in 1..4 for every beat, n == 1 on
    every emitted downbeat, and a cycle that only ever advances or wraps. That
    is asserted below and it fails if any part of the chain breaks.

    THE INVARIANT CHANGED WITH THE FIX, AND THE OLD ONE WAS THE DEFECT. This
    test used to assert `n1 == n0 + 1` between downbeats, an unbounded count
    that `beat_this.utils.infer_beat_numbers` satisfies by emitting 5, 6, 7, 8
    when a downbeat is missed. It passed on this click track for the same
    reason the defect survived review: a track whose downbeats are all present
    never exercises the wrap. So the synthetic unit case in
    `tests/analysis_beatgrid/test_bar_phase.py` is what pins the wrap, and this
    test pins that the real chain still produces a legal cycle end to end.
    """
    result = analyzed["payload"]["results"][analyzed["steady"]]
    beats = result["beats"]
    lock = lock_bar_phase(beats, result["downbeats"])
    numbers = lock.beat_numbers

    assert numbers, "lock_bar_phase produced nothing"
    assert not lock.bar_phase_unestablished
    assert len(numbers) == len(beats)
    assert all(1 <= n <= BAR_BEATS for n in numbers), f"max n {max(numbers)} exceeds a bar"

    bar_one_times = [t for t, n in zip(beats, numbers, strict=True) if n == 1]
    for earlier, later in itertools.pairwise(bar_one_times):
        earlier_index = beats.index(earlier)
        later_index = beats.index(later)
        assert later_index - earlier_index == BAR_BEATS, (
            f"served bar-1 at {earlier}s and {later}s are {later_index - earlier_index} "
            "beats apart, expected 4"
        )


def test_the_click_track_downbeat_density_is_recorded_not_assumed(analyzed):
    """Pins the finding above so a future model change is visible, not silent.

    If a later Beat This! release, or a threshold change, made downbeat output
    on synthetic audio sane, this test goes red and the finding recorded in the
    docstring above needs rewriting. That is the point: a measured property
    nobody re-measures becomes a belief.
    """
    result = analyzed["payload"]["results"][analyzed["steady"]]
    true_downbeats = analyzed["truth"][::4]
    emitted = len(result["downbeats"])
    ratio = emitted / len(true_downbeats)
    assert ratio > 1.4, (
        f"Beat This! emitted {emitted} downbeats against {len(true_downbeats)} true "
        f"accents (ratio {ratio:.2f}). Round 1 measured 1.79. A ratio near 1.0 means "
        f"the model now reads bar structure from a metronome, which would be a real "
        f"improvement and makes the finding in the test above stale."
    )


def test_a_three_percent_step_is_detected_within_eight_bars(analyzed):
    """The EASIER half of acceptance line 2: an abrupt change, end to end.

    Kept beside the gradual ramp below rather than replaced by it, so a failure
    says WHICH capability is missing: a detector that fails both cannot see a
    tempo change at all, while one that passes here and fails there sees only
    discontinuities.
    """
    result = analyzed["payload"]["results"][analyzed["stepped"]]
    assert result["error"] is None
    analysis = detect_tempo_changes(result["beats"])

    assert analysis.static_grid_untrusted is True, "the injected step was not detected"
    tolerance_s = MIN_SEGMENT_BARS * 4 * (60.0 / CLICK_BPM)
    near = [m for m in analysis.markers if abs(m.at_s - RAMP_AT_S) <= tolerance_s]
    assert near, (
        f"no marker within {tolerance_s:.1f}s of {RAMP_AT_S}s; "
        f"markers at {[round(m.at_s, 1) for m in analysis.markers]}"
    )
    marker = near[0]
    assert marker.bpm_after > marker.bpm_before
    assert marker.bpm_after / marker.bpm_before == pytest.approx(RAMP_FACTOR, rel=0.02)


def test_a_three_percent_gradual_ramp_is_detected_within_eight_bars(analyzed):
    """NATIVE-03 as the spec actually words it: a RAMP at 2:00, not a step.

    This is the case the previous version of this module did not cover. A step
    leaves one anomalous inter-beat interval at the splice, which a detector can
    find without following tempo at all; a ramp spread over 8 bars changes every
    interval by a fraction of a percent and can only be found by comparing
    segments. Passing the step and failing this would mean the detector reads
    discontinuities rather than tempo, which is what NATIVE-03 rules out.

    The marker is required within 8 bars of where the ramp BEGINS. A ramp has no
    single instant to point at, so the honest target is its onset: a detector
    that reported the ramp's midpoint or its end would be describing the same
    event, and 8 bars at 128 BPM is 15.0 s, which spans the ramp.
    """
    result = analyzed["payload"]["results"][analyzed["ramped"]]
    assert result["error"] is None
    analysis = detect_tempo_changes(result["beats"])

    assert analysis.static_grid_untrusted is True, "the gradual ramp was not detected"
    tolerance_s = MIN_SEGMENT_BARS * 4 * (60.0 / CLICK_BPM)
    near = [m for m in analysis.markers if abs(m.at_s - RAMP_AT_S) <= tolerance_s]
    assert near, (
        f"no marker within {tolerance_s:.1f}s of the ramp onset at {RAMP_AT_S}s; "
        f"markers at {[round(m.at_s, 1) for m in analysis.markers]}"
    )
    marker = near[0]
    assert marker.bpm_after > marker.bpm_before, "the ramp rises, so the marker must too"


def test_the_ramp_fixture_really_ramps_rather_than_stepping(analyzed):
    """A control on the INSTRUMENT, not on the detector.

    If the generator produced a step after all, the test above would pass while
    measuring the wrong thing, which is the exact defect being fixed. So the
    written beat times are checked directly: no single inter-beat interval may
    jump by the full 3 percent, and the tempo at the end must be 3 percent above
    the tempo at the start.
    """
    times = analyzed["ramp_truth"]
    intervals = [b - a for a, b in itertools.pairwise(times)]
    steps = [abs(b - a) / a for a, b in itertools.pairwise(intervals)]

    assert max(steps) < 0.01, (
        f"largest single interval jump {max(steps):.4%}; a 3 percent change arriving "
        f"in one interval is a step, not a ramp"
    )
    first, last = 60.0 / intervals[0], 60.0 / intervals[-1]
    assert last / first == pytest.approx(RAMP_FACTOR, rel=0.01), (
        f"tempo went {first:.2f} -> {last:.2f} BPM, expected a factor of {RAMP_FACTOR}"
    )


def test_a_four_percent_ramp_emits_tempo_map_anchors_bracketing_the_ramp(analyzed):
    """[if] a real 4 percent ramp between 1:30 and 2:00 [then] the map brackets it."""
    result = analyzed["payload"]["results"][analyzed["ramped_4pct"]]
    assert result["error"] is None
    ref = {"npz": result["activations_npz"], "fps": result.get("fps", 50)}
    tempo_map = fit_tempo_map(ref, result["beats"])
    anchors = tempo_map.anchors
    assert len(anchors) >= 2
    assert any(a.at_s <= 90.0 for a in anchors)
    tolerance_s = MIN_SEGMENT_BARS * 4 * (60.0 / CLICK_BPM)
    later = [a for a in anchors if 90.0 - tolerance_s <= a.at_s <= 120.0 + tolerance_s]
    assert later, (
        f"no anchor near ramp window; anchors at {[round(a.at_s, 1) for a in anchors]}"
    )
    assert later[-1].bpm > anchors[0].bpm


def test_the_untouched_click_track_yields_one_tempo_map_anchor(analyzed):
    """[if] the click track is fixed tempo [then] the v2 fitter returns one anchor."""
    result = analyzed["payload"]["results"][analyzed["steady"]]
    assert result["error"] is None
    ref = {"npz": result["activations_npz"], "fps": result.get("fps", 50)}
    tempo_map = fit_tempo_map(ref, result["beats"])
    assert len(tempo_map.anchors) == 1


def test_the_untouched_track_yields_zero_markers(analyzed):
    """Acceptance line 3, the control that makes line 2 mean something.

    Same detector, same model, same click track, and the only difference is
    that no step was injected. A detector that fired on everything would pass
    the previous test and fail this one.
    """
    result = analyzed["payload"]["results"][analyzed["steady"]]
    analysis = detect_tempo_changes(result["beats"])
    detail = [
        (round(m.at_s, 1), round(m.bpm_before, 2), round(m.bpm_after, 2))
        for m in analysis.markers
    ]
    assert analysis.markers == (), f"false positives on an unmutated track: {detail}"
    assert analysis.static_grid_untrusted is False


# ----- Exit code ----------------------------------------------------------


def test_a_run_where_every_track_failed_exits_nonzero(tmp_path) -> None:
    """If a run with zero successes exits 0, then an empty shard reads as done, or broken.

    Found by measurement rather than review, on PR #1660: a mis-expanded shell
    argument sent one bogus path, the runner printed `done: 0/1` and returned
    0, and the artifact it wrote had no results at all. In a sharded backfill
    that is a missing shard the merge accepts, which is the exit-code
    false-green `.claude/rules/verification.md` names first.
    """
    out = str(tmp_path / "allfail.json")
    result = subprocess.run(
        ["uv", "run", "--no-project", "--script", RUNNER,
         "--out", out, "--device", "cpu", "--audio", str(tmp_path / "absent.wav")],
        check=False, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode != 0, "a run that produced nothing reported success"
    assert "all 1 track(s) errored" in result.stderr


def test_a_run_with_SOME_failures_still_exits_zero(tmp_path) -> None:
    """If one bad track fails the whole run, then round 2's corpus is unrunnable, or broken.

    The control for the test above, and the one that matters more, because the
    plausible mistake here is the OVERSHOOT. GTZAN's corrupt `jazz.00054` fails
    on every legitimate run of the committed round-2 corpus, so a rule of "any
    failure is fatal" would refuse the benchmark this repository is built
    around while satisfying the bug report perfectly.
    """
    good = str(tmp_path / "good.wav")
    _render_clicks(good, _steady_beat_times(CLICK_BPM, 8.0), 8.0)
    out = str(tmp_path / "partial.json")
    result = subprocess.run(
        ["uv", "run", "--no-project", "--script", RUNNER,
         "--out", out, "--device", "cpu",
         "--audio", good, str(tmp_path / "absent.wav")],
        check=False, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    with open(out, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["n_tracks"] == 2
    assert payload["n_failed"] == 1
