"""The waveform lane adapter: correlation delegated to score.py v1.0.0."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis_bench import bundles, controls
from apps.analysis_bench.scorers import waveform_lane
from apps.analysis_waveform import score as waveform_score
from apps.analysis_waveform.decode import BAND_NAMES
from tests.analysis_bench import synthetic_waveform


def _arm(role: str, results: dict) -> dict:
    return {"role": role, "note": "", "payload": {"results": results}}


@pytest.fixture()
def waveform_bundle(tmp_path: Path) -> Path:
    staged = tmp_path / "waveform-v1"
    tracks = synthetic_waveform.build(staged)
    bundles.seal_bundle(
        staged,
        lane="waveform",
        version="v1",
        manifest_extra=synthetic_waveform.manifest_extra(tracks),
    )
    return staged


def test_pearson_is_delegated_to_the_versioned_scorer() -> None:
    assert waveform_lane.pearson is waveform_score.pearson


def test_truth_echo_median_r_is_one_on_every_band(waveform_bundle: Path) -> None:
    manifest_path = waveform_bundle / "manifest.json"
    positive = controls._waveform_positive(manifest_path)
    assert positive["candidate"] == "truth_echo"
    report = waveform_lane.score_bundle(
        waveform_bundle, {"truth_echo": _arm("positive_control", positive["results"])}
    )
    arm = report["arms"]["truth_echo"]
    for name in BAND_NAMES:
        assert abs(arm[name]["median"] - 1.0) <= waveform_score.POSITIVE_CONTROL_TOLERANCE
        assert arm[name]["n"] == 2


def test_constant_band_median_r_is_zero_and_stays_in_n(waveform_bundle: Path) -> None:
    manifest_path = waveform_bundle / "manifest.json"
    negative = controls._waveform_negative(manifest_path)
    assert negative["candidate"] == "constant_band"
    report = waveform_lane.score_bundle(
        waveform_bundle,
        {"constant_band": _arm("negative_control", negative["results"])},
    )
    arm = report["arms"]["constant_band"]
    assert arm["n_omitted"] == 0
    for name in BAND_NAMES:
        assert arm[name]["median"] == 0.0
        assert arm[name]["n"] == 2


def test_an_omitted_fixture_drags_the_median_down(waveform_bundle: Path) -> None:
    manifest_path = waveform_bundle / "manifest.json"
    positive = controls._waveform_positive(manifest_path)
    one = {
        "synthetic-ramp": positive["results"]["synthetic-ramp"],
    }
    report = waveform_lane.score_bundle(
        waveform_bundle, {"candidate": _arm("candidate", one)}
    )
    arm = report["arms"]["candidate"]
    assert arm["n_omitted"] == 1
    for name in BAND_NAMES:
        assert arm[name]["n"] == 2
        assert arm[name]["median"] < 1.0


def test_an_arm_answering_a_foreign_fixture_is_refused(waveform_bundle: Path) -> None:
    with pytest.raises(ValueError, match="does not contain"):
        waveform_lane.score_bundle(
            waveform_bundle,
            {
                "candidate": _arm(
                    "candidate",
                    {"not-in-this-bundle": {"bands": {"low": [0.1], "mid": [0.1], "high": [0.1]}}},
                )
            },
        )


def test_render_table_matches_gfm_cells_and_sorts_controls_first(
    waveform_bundle: Path,
) -> None:
    manifest_path = waveform_bundle / "manifest.json"
    negative = controls._waveform_negative(manifest_path)
    positive = controls._waveform_positive(manifest_path)
    report = waveform_lane.score_bundle(
        waveform_bundle,
        {
            "truth_echo": _arm("positive_control", positive["results"]),
            "constant_band": _arm("negative_control", negative["results"]),
            "other": _arm("candidate", positive["results"]),
        },
    )
    text = waveform_lane.render_table(report)
    header = next(line for line in text.splitlines() if line.startswith("| arm"))
    delimiter = text.splitlines()[text.splitlines().index(header) + 1]
    assert header.count("|") == delimiter.count("|")
    assert set(delimiter.strip("|").split("|")) == {"---"}
    names = [
        line.split("|")[1].strip()
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith("| arm") and "---" not in line
    ]
    assert names == ["constant_band", "truth_echo", "other"]
