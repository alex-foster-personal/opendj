"""The key lane through the actual CLI: seal, push, pull, run, score, post.

The beatgrid suite (`test_cli_end_to_end.py`) already exercises the
lane-agnostic harness machinery (tampered bundles, stale arms, foreign
fixtures, cross-lane and cross-version refusals) against a beatgrid bundle;
those code paths are shared, not reimplemented per lane, so this file does
not repeat them. It proves the one thing that IS lane-specific: `--lane key`
now runs and posts a round instead of refusing by name (lanes.py's
`scorer_module` flip), using the three controls as the round's only arms
since NATIVE-04's producer half (a real candidate) is not built yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_bench import bundles, cli
from tests.analysis_bench import synthetic_key


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    staged = tmp_path / "key-v1"
    fixtures = synthetic_key.build(staged)
    bundles.seal_bundle(
        staged, lane="key", version="v1", manifest_extra=synthetic_key.manifest_extra(fixtures)
    )
    return staged


def test_seal_push_pull_run_and_post(bundle: Path, tmp_path: Path) -> None:
    store_root = tmp_path / "asset-store"
    assert cli.main(["fixtures", "push", "--lane", "key", "--version", "v1",
                     "--bundle-dir", str(bundle), "--store", str(store_root)]) == 0
    pulled = tmp_path / "pulled"
    assert cli.main(["fixtures", "pull", "--lane", "key", "--version", "v1",
                     "--dest", str(pulled), "--store", str(store_root)]) == 0

    log = tmp_path / "spec.md"
    log.write_text("# spec\n\n## Experiment log\n")
    report_path = tmp_path / "report.json"
    # truth_echo stands in for the missing candidate role (see module docstring):
    # its own arm role stays positive_control, constant_key is added as the
    # round's floor, and truth_echo_mik (the MIK-side ceiling) comes along
    # too since it is one of this lane's declared controls.
    assert cli.main(["run", "--lane", "key", "--candidate", "truth_echo",
                     "--bundle-dir", str(pulled), "--workdir", str(tmp_path / "arms"),
                     "--out", str(report_path), "--post", "--log-path", str(log)]) == 0

    report = json.loads(report_path.read_text())
    assert report["scorer_version"] == "1.0.0"
    assert set(report["arms"]) == {"truth_echo", "truth_echo_mik", "constant_key"}
    assert report["bundle"]["bundle_id"] == bundles.verify_bundle(bundle)["bundle_id"]

    ceiling = report["arms"]["truth_echo"]["vs_rekordbox"]["mirex_mean_pct"]
    floor = report["arms"]["constant_key"]["vs_rekordbox"]["mirex_mean_pct"]
    assert ceiling - floor > 5.0
    # truth_echo_mik is the genuine ceiling for the vs_mik columns: truth_echo
    # itself only echoes rekordbox, so it is not one (Codex P2 BLOCKING, PR
    # #1620).
    assert report["arms"]["truth_echo_mik"]["vs_mik"]["mirex_mean_pct"] == 100.0

    text = log.read_text()
    assert "### key bench round 0" in text
    assert "scorer 1.0.0" in text
    assert "constant_key (negative_control)" in text
    assert "truth_echo_mik (positive_mik_control)" in text
    assert "Key Signature Estimation Accuracy" in text


def test_run_without_controls_cannot_post(bundle: Path, tmp_path: Path, capsys) -> None:
    log = tmp_path / "spec.md"
    log.write_text("# spec\n")
    code = cli.main(["run", "--lane", "key", "--candidate", "constant_key",
                     "--bundle-dir", str(bundle), "--workdir", str(tmp_path / "arms"),
                     "--no-controls", "--post", "--log-path", str(log)])
    assert code == 2
    assert "cannot be posted" in capsys.readouterr().err
    assert log.read_text() == "# spec\n"
