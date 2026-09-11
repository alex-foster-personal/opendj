"""The waveform lane through the actual CLI: seal, push, pull, run, score, post.

The beatgrid suite (`test_cli_end_to_end.py`) already exercises the
lane-agnostic harness machinery (tampered bundles, stale arms, foreign
fixtures, cross-lane and cross-version refusals) against a beatgrid bundle;
those code paths are shared, not reimplemented per lane, so this file does
not repeat them. It proves the one thing that IS lane-specific: `--lane
waveform` now runs and posts a round instead of refusing by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_bench import bundles, cli
from tests.analysis_bench import synthetic_waveform


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    staged = tmp_path / "waveform-v1"
    tracks = synthetic_waveform.build(staged)
    bundles.seal_bundle(
        staged,
        lane="waveform",
        version="v1",
        manifest_extra=synthetic_waveform.manifest_extra(tracks),
    )
    return staged


def test_seal_push_pull_run_and_post(bundle: Path, tmp_path: Path) -> None:
    store_root = tmp_path / "asset-store"
    assert cli.main(["fixtures", "push", "--lane", "waveform", "--version", "v1",
                     "--bundle-dir", str(bundle), "--store", str(store_root)]) == 0
    pulled = tmp_path / "pulled"
    assert cli.main(["fixtures", "pull", "--lane", "waveform", "--version", "v1",
                     "--dest", str(pulled), "--store", str(store_root)]) == 0

    log = tmp_path / "spec.md"
    log.write_text("# spec\n\n## Experiment log\n")
    report_path = tmp_path / "report.json"
    assert cli.main(["run", "--lane", "waveform", "--candidate", "truth_echo",
                     "--bundle-dir", str(pulled), "--workdir", str(tmp_path / "arms"),
                     "--out", str(report_path), "--post", "--log-path", str(log)]) == 0

    report = json.loads(report_path.read_text())
    assert report["scorer_version"] == "1.0.0"
    assert set(report["arms"]) == {"truth_echo", "constant_band"}
    assert report["bundle"]["bundle_id"] == bundles.verify_bundle(bundle)["bundle_id"]

    text = log.read_text()
    assert "### waveform bench round 0" in text
    assert "scorer 1.0.0" in text
    assert "constant_band (negative_control)" in text


def test_run_without_controls_cannot_post(bundle: Path, tmp_path: Path, capsys) -> None:
    log = tmp_path / "spec.md"
    log.write_text("# spec\n")
    code = cli.main(["run", "--lane", "waveform", "--candidate", "constant_band",
                     "--bundle-dir", str(bundle), "--workdir", str(tmp_path / "arms"),
                     "--no-controls", "--post", "--log-path", str(log)])
    assert code == 2
    assert "cannot be posted" in capsys.readouterr().err
    assert log.read_text() == "# spec\n"
