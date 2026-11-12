"""Low-confidence beat-grid regions in the beatgrid analysis JSON and CLI (STANDALONE-12242).

[if] a grid has a localized uneven span [then] the analysis CLI reports its bounds and score, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_beatgrid.cli import analyze, main
from apps.analysis_beatgrid.grid_quality import flagged_block_share, low_confidence_regions

pytestmark = pytest.mark.requirement("STANDALONE-12242")

CONFORMANCE: Path = Path(__file__).resolve().parent / "fixtures" / "grid_quality_conformance.json"
PERIOD_S: float = 60.0 / 126.0
NUDGED_BEAT: int = 200


def _steady_beats(n: int = 400) -> list[float]:
    return [round(0.02 + i * PERIOD_S, 3) for i in range(n)]


def _nudged_beats() -> list[float]:
    """Synthesized: a steady 126 BPM grid with one beat moved 12 ms late."""
    beats = _steady_beats()
    beats[NUDGED_BEAT] = round(beats[NUDGED_BEAT] + 0.012, 3)
    return beats


def _payload(beats: list[float]) -> dict[str, object]:
    """Synthesized beat_this_runner.py output for one track."""
    return {
        "producer": "test",
        "producer_version": "0",
        "device": "cpu",
        "threshold": 0.5,
        "results": {"track.wav": {"beats": beats, "downbeats": beats[::4], "activation_peak": 0.9}},
    }


def test_localized_span_reaches_json_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a localized low-confidence span exists [then] JSON and CLI carry start, end and score, [else stop]."""
    beats = _nudged_beats()
    beats_path = tmp_path / "beats.json"
    out_path = tmp_path / "out.json"
    beats_path.write_text(json.dumps(_payload(beats)), encoding="utf-8")
    assert main(["--beats", str(beats_path), "--out", str(out_path)]) == 0
    analysis = json.loads(out_path.read_text(encoding="utf-8"))["analyses"]["track.wav"]
    confidence = analysis["grid_confidence"]
    assert confidence["status"] == "low_confidence"
    [region] = confidence["regions"]
    assert region["start_s"] == beats[NUDGED_BEAT - 1]
    assert region["end_s"] == beats[NUDGED_BEAT + 1]
    assert 0.0 <= region["score"] < 1.0
    printed = capsys.readouterr().out
    assert f"low confidence {region['start_s']:.3f}s-{region['end_s']:.3f}s (score {region['score']:.3f})" in printed


def test_known_good_conformance_set_stays_under_ten_percent() -> None:
    """[if] the known-good conformance grids are analyzed [then] at most 10 percent of blocks flag, [else stop]."""
    cases = json.loads(CONFORMANCE.read_text(encoding="utf-8"))["cases"]
    known_good = [case for case in cases if case["expected"]["grid_class"] == "ok"]
    assert len(known_good) >= 3
    for case in known_good:
        confidence = low_confidence_regions(case["beat_times"])
        assert flagged_block_share(case["beat_times"], confidence) <= 0.10, case["name"]


def test_clean_grid_invents_no_region(tmp_path: Path) -> None:
    """[if] no low-confidence region exists [then] the output has none and reads clean, [else stop]."""
    analysis = analyze(_payload(_steady_beats())["results"]["track.wav"], threshold=0.5)  # type: ignore[index]
    assert analysis["status"] == "ok"
    assert analysis["grid_confidence"] == {"status": "clean", "reason": None, "regions": []}


@pytest.mark.parametrize(
    ("beats", "reason"),
    [
        ([0.5, float("nan"), 1.5], "non_finite_beat_time"),
        ([0.5, 1.0, 1.0, 1.5], "beat_times_not_increasing"),
        ([0.5, 1.5, 1.0], "beat_times_not_increasing"),
        ([0.5], "too_few_beats"),
    ],
)
def test_invalid_input_never_reads_clean(beats: list[float], reason: str) -> None:
    """[if] beat times are invalid or incomplete [then] the result is invalid, never clean, [else stop]."""
    confidence = low_confidence_regions(beats)
    assert confidence.status == "invalid"
    assert confidence.reason == reason
    if reason != "too_few_beats":
        steady = _steady_beats(40)
        steady[10 : 10 + len(beats)] = beats
        analysis = analyze({"beats": steady, "downbeats": steady[::4], "activation_peak": 0.9}, threshold=0.5)
        assert analysis["status"] == "failed"
        assert analysis["reason"] == reason
