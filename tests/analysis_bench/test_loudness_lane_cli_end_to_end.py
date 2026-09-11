"""The loudness lane through the actual CLI: seal, push, pull, run, score, post.

The beatgrid suite (`test_cli_end_to_end.py`) already exercises the
lane-agnostic harness machinery (tampered bundles, stale arms, foreign
fixtures, cross-lane and cross-version refusals) against a beatgrid bundle;
those code paths are shared, not reimplemented per lane, so this file does
not repeat them. It proves the one thing that IS lane-specific: `--lane
loudness` now runs and posts a round instead of refusing by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_bench import bundles, cli
from tests.analysis_bench import synthetic_loudness


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    staged = tmp_path / "loudness-v1"
    fixtures = synthetic_loudness.build(staged)
    bundles.seal_bundle(
        staged,
        lane="loudness",
        version="v1",
        manifest_extra=synthetic_loudness.manifest_extra(fixtures),
    )
    return staged


def test_seal_push_pull_run_and_post(bundle: Path, tmp_path: Path) -> None:
    store_root = tmp_path / "asset-store"
    assert cli.main(["fixtures", "push", "--lane", "loudness", "--version", "v1",
                     "--bundle-dir", str(bundle), "--store", str(store_root)]) == 0
    pulled = tmp_path / "pulled"
    assert cli.main(["fixtures", "pull", "--lane", "loudness", "--version", "v1",
                     "--dest", str(pulled), "--store", str(store_root)]) == 0

    log = tmp_path / "spec.md"
    log.write_text("# spec\n\n## Experiment log\n")
    report_path = tmp_path / "report.json"
    assert cli.main(["run", "--lane", "loudness", "--candidate", "truth_echo",
                     "--bundle-dir", str(pulled), "--workdir", str(tmp_path / "arms"),
                     "--out", str(report_path), "--post", "--log-path", str(log)]) == 0

    report = json.loads(report_path.read_text())
    assert report["scorer_version"] == "1.0.0"
    assert set(report["arms"]) == {"truth_echo", "constant_lufs"}
    assert report["bundle"]["bundle_id"] == bundles.verify_bundle(bundle)["bundle_id"]
    assert report["arms"]["truth_echo"]["gate_pass"] is True
    assert report["arms"]["constant_lufs"]["gate_pass"] is False

    text = log.read_text()
    assert "### loudness bench round 0" in text
    assert "scorer 1.0.0" in text
    assert "constant_lufs (negative_control)" in text


def test_run_without_controls_cannot_post(bundle: Path, tmp_path: Path, capsys) -> None:
    log = tmp_path / "spec.md"
    log.write_text("# spec\n")
    code = cli.main(["run", "--lane", "loudness", "--candidate", "constant_lufs",
                     "--bundle-dir", str(bundle), "--workdir", str(tmp_path / "arms"),
                     "--no-controls", "--post", "--log-path", str(log)])
    assert code == 2
    assert "cannot be posted" in capsys.readouterr().err
    assert log.read_text() == "# spec\n"
