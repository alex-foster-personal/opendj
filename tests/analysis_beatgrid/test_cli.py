"""The second half of the producer: what the CLI's payload must actually carry.

TWO REVIEW FINDINGS ARE PINNED HERE, both from Codex on PR #1514.

THE PAYLOAD MUST CARRY THE BAR PHASE (P1). `bar_phase.assign_bar_phase` existed
and was exercised only by the benchmark reshaper, so a caller following the
documented runner-to-CLI path got beat and downbeat COUNTS and no 1..4 phase at
all, which is the one thing strict BAR sync needs. Counting the numbers is not
enough on its own: a test that only asserted `beat_numbers` is non-empty would
pass against the unwrapped 1..8 numbering the phase work replaced, so the range
assertion is the one that bites.

THE THRESHOLD MUST TRAVEL WITH THE RUN (P2). The runner's peak picker keeps a
frame at whatever `--threshold` it was given; `evaluate_pulse` used to re-judge
the result against a hard 0.5. Between 0.35 and 0.5 the two halves of one
producer disagreed, and the disagreement is invisible at the default because
0.5 is also the default. So the test drives the SAME track through both
thresholds and requires the verdict to move: a fixed-threshold implementation
fails it in one direction, and an implementation that ignores the threshold
entirely fails it in the other.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from apps.analysis_beatgrid.bar_phase import BAR_BEATS
from apps.analysis_beatgrid.cli import analyze, main

BEAT_S = 0.5  # 120 BPM, so the grid is unambiguous and the octave logic is idle


def _grid(n_beats: int) -> list[float]:
    return [i * BEAT_S for i in range(n_beats)]


def _result(
    n_beats: int = 24,
    *,
    downbeat_every: int | None = 4,
    activation_peak: float = 0.9,
    drop_downbeat_index: int | None = None,
) -> dict[str, Any]:
    beats = _grid(n_beats)
    if downbeat_every is None:
        downbeats: list[float] | None = None
    else:
        anchors = beats[::downbeat_every]
        if drop_downbeat_index is not None:
            anchors = [t for i, t in enumerate(anchors) if i != drop_downbeat_index]
        downbeats = anchors
    return {"beats": beats, "downbeats": downbeats, "activation_peak": activation_peak}


# ----- the bar phase reaches the payload ----------------------------------


def test_payload_carries_a_bar_number_for_every_beat_inside_1_to_4() -> None:
    analysis = analyze(_result(), threshold=0.5)

    assert analysis["status"] == "ok"
    numbers = analysis["beat_numbers"]
    assert len(numbers) == analysis["beats"] == 24
    assert set(numbers) == {1, 2, 3, 4}
    assert all(1 <= n <= BAR_BEATS for n in numbers)
    assert analysis["bar_phase_unestablished"] is False
    assert analysis["bar_phase_reason"] is None
    assert numbers[:5] == [1, 2, 3, 4, 1]


def test_a_missed_downbeat_is_reported_rather_than_hidden_by_the_wrap() -> None:
    """The wrap must not make a 8-beat bar indistinguishable from two real bars."""
    analysis = analyze(_result(drop_downbeat_index=2), threshold=0.5)

    assert set(analysis["beat_numbers"]) == {1, 2, 3, 4}
    assert analysis["n_bars_over_length"] == 1
    assert analysis["max_bar_beats"] == 8


def test_an_analyzer_with_no_downbeat_concept_gets_no_invented_phase() -> None:
    analysis = analyze(_result(downbeat_every=None), threshold=0.5)

    assert analysis["status"] == "ok"
    assert analysis["beat_numbers"] == []
    assert analysis["bar_phase_unestablished"] is True
    assert analysis["bar_phase_reason"] == "no_downbeat_anchor"


def test_a_grid_with_beats_but_zero_downbeats_fails_before_the_phase() -> None:
    result = _result()
    result["downbeats"] = []
    analysis = analyze(result, threshold=0.5)

    assert analysis["status"] == "failed"
    assert analysis["reason"] == "no_downbeat_anchor"


# ----- the producer's threshold is the one applied ------------------------


def test_the_same_track_passes_at_0_35_and_fails_at_0_5() -> None:
    borderline = _result(activation_peak=0.4)

    at_default = analyze(borderline, threshold=0.5)
    at_lower_arm = analyze(borderline, threshold=0.35)

    assert at_default["status"] == "failed"
    assert at_default["reason"] == "activation_below_threshold"
    assert at_lower_arm["status"] == "ok", (
        "a track the producer's picker accepted at 0.35 was rejected downstream"
    )
    assert at_lower_arm["beat_numbers"]


def test_analyze_refuses_to_be_called_without_a_threshold() -> None:
    with pytest.raises(TypeError):
        analyze(_result())  # type: ignore[call-arg]


# ----- end to end through the CLI -----------------------------------------


def _write_payload(path: str, *, threshold: float | None, peak: float) -> None:
    payload: dict[str, Any] = {
        "producer": "beat_this",
        "producer_version": "1.1.0",
        "device": "cpu",
        "results": {"/music/track.wav": _result(activation_peak=peak)},
    }
    if threshold is not None:
        payload["threshold"] = threshold
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_main_applies_the_threshold_recorded_in_the_payload(tmp_path, capsys) -> None:
    beats_path = str(tmp_path / "beats.json")
    out_path = str(tmp_path / "analyses.json")
    _write_payload(beats_path, threshold=0.35, peak=0.4)

    assert main(["--beats", beats_path, "--out", out_path]) == 0

    with open(out_path, encoding="utf-8") as fh:
        analyses = json.load(fh)["analyses"]
    analysis = analyses["/music/track.wav"]
    assert analysis["status"] == "ok"
    assert set(analysis["beat_numbers"]) == {1, 2, 3, 4}
    assert "bar phase" in capsys.readouterr().out


def test_main_refuses_one_prior_bpm_across_several_tracks(tmp_path) -> None:
    """`--prior-bpm` is ONE track's rekordbox figure, so it cannot describe many.

    Applying it across a multi-track payload would push every metrically
    ambiguous track towards ANOTHER track's tempo, which is a corrupted scoring
    analysis that still looks like a table (Codex P2 on PR #1514).
    """
    beats_path = str(tmp_path / "beats.json")
    payload = {
        "threshold": 0.5,
        "results": {"/music/a.wav": _result(), "/music/b.wav": _result()},
    }
    with open(beats_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    with pytest.raises(SystemExit) as excinfo:
        main(["--beats", beats_path, "--prior-bpm", "128"])
    assert "holds 2 results" in str(excinfo.value)


def test_a_single_track_payload_still_accepts_a_prior(tmp_path) -> None:
    """The control: the refusal must not disable the scoring path it guards."""
    beats_path = str(tmp_path / "beats.json")
    out_path = str(tmp_path / "out.json")
    _write_payload(beats_path, threshold=0.5, peak=0.9)

    assert main(["--beats", beats_path, "--prior-bpm", "120", "--out", out_path]) == 0

    with open(out_path, encoding="utf-8") as fh:
        analysis = json.load(fh)["analyses"]["/music/track.wav"]
    assert analysis["status"] == "ok"


def test_main_refuses_a_payload_that_records_no_threshold(tmp_path) -> None:
    """The negative control: a threshold-less payload must not be judged at 0.5."""
    beats_path = str(tmp_path / "beats.json")
    _write_payload(beats_path, threshold=None, peak=0.9)

    with pytest.raises(SystemExit) as excinfo:
        main(["--beats", beats_path])
    assert "records no producer `threshold`" in str(excinfo.value)
