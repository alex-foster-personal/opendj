"""Three-host round tables and promotion refusal (NATIVE-12070, issue #11227)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_bench import parity, rounds
from tests.analysis_bench import parity_fixtures


def _report_with_hosts(**overrides: object) -> dict:
    base = {
        "scorer_version": "1.1.0",
        "bundle": {"lane": "beatgrid", "version": "v1", "bundle_id": "fixture-bundle"},
        "table": "| candidate | n |\n|---|---|\n| beat_this | 90 |",
        "arms": {
            "beat_this": {"role": "candidate", "fixed": {"f_measure_mean": 0.9}},
            "truth_offset": {"role": "positive_control"},
            "constant_128": {"role": "negative_control"},
        },
    }
    base.update(overrides)
    return parity_fixtures.attach_synthetic_hosts(base)


# REQ: NATIVE-12070
@pytest.mark.requirement("NATIVE-12070")
def test_append_refuses_fewer_than_three_host_columns(tmp_path: Path) -> None:
    """[if] a round table has fewer than three host columns [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["table"] = (
        "| metric | macOS arm64 | Windows x86_64 native |\n|---|---|---|\n| parity | pending | pending |\n"
    )
    with pytest.raises(rounds.RoundError, match="fewer than three host columns"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12070")
def test_append_refuses_macos_column_without_arm64(tmp_path: Path) -> None:
    """[if] macOS column machine is not arm64 [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_MACOS_ARM64]["identity"]["machine"] = "x86_64"
    with pytest.raises(rounds.RoundError, match="arm64"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12070")
def test_append_refuses_windows_wsl_column(tmp_path: Path) -> None:
    """[if] Windows column is WSL not native x86_64 [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_WINDOWS_X86_64]["identity"]["wsl"] = True
    with pytest.raises(rounds.RoundError, match="WSL"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12070")
def test_promotion_refuses_when_latest_round_has_no_parity_row(tmp_path: Path) -> None:
    """[if] promotion is requested with no parity row [then] refuse, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text(
        "## Experiment log\n\n"
        "### beatgrid bench round 0\n\n"
        "| metric | macOS arm64 | Linux x86_64 | Windows x86_64 native |\n"
        "|---|---|---|---|\n"
        "| scorer | 1.0.0 | 1.0.0 | 1.0.0 |\n"
    )
    with pytest.raises(rounds.RoundError, match="parity row"):
        parity.assert_promotion_allowed(log, "beatgrid")


@pytest.mark.requirement("NATIVE-12070")
def test_promotion_allowed_when_matrix_and_parity_row_present(tmp_path: Path) -> None:
    """[if] latest round has three host columns and parity row [then] promotion proceeds, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    rounds.append_round(log, "beatgrid", report, floor=0, host="fixture-linux")
    parity.assert_promotion_allowed(log, "beatgrid")


@pytest.mark.requirement("NATIVE-12070")
def test_run_post_refuses_without_merged_host_reports(bundle: Path, tmp_path: Path, capsys) -> None:
    """[if] run --post has only this host's column [then] refuse, [else stop]."""
    from apps.analysis_bench import cli

    log = tmp_path / "spec.md"
    log.write_text("# spec\n\n## Experiment log\n")
    code = cli.main(
        [
            "run",
            "--lane",
            "beatgrid",
            "--candidate",
            "constant_128",
            "--bundle-dir",
            str(bundle),
            "--workdir",
            str(tmp_path / "arms"),
            "--post",
            "--log-path",
            str(log),
        ]
    )
    assert code == 2
    assert "host column" in capsys.readouterr().err.lower()
    assert log.read_text().endswith("## Experiment log\n")


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    from apps.analysis_bench import bundles
    from tests.analysis_bench import synthetic

    staged = tmp_path / "beatgrid-v1"
    fixtures = synthetic.build(staged)
    bundles.seal_bundle(staged, lane="beatgrid", version="v1", manifest_extra=synthetic.manifest_extra(fixtures))
    return staged


def test_host_reports_json_round_trip(tmp_path: Path) -> None:
    """Merged host-reports JSON satisfies post validation."""
    from apps.analysis_bench import cli

    report = _report_with_hosts()
    host_reports_path = tmp_path / "hosts.json"
    host_reports_path.write_text(json.dumps(report["host_reports"]), encoding="utf-8")
    parity.validate_host_reports(
        json.loads(host_reports_path.read_text()),
        report=report,
    )
    prepared: dict = {
        "scorer_version": report["scorer_version"],
        "bundle": report["bundle"],
        "arms": report["arms"],
        "table": "lane",
    }
    cli._prepare_report_for_post(prepared, host_reports_path=str(host_reports_path))
    parity.validate_posted_table(prepared["table"])


@pytest.mark.requirement("NATIVE-12159")
def test_append_refuses_host_identity_without_arms(tmp_path: Path) -> None:
    """[if] a host column has identity only [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_LINUX_X86_64] = {
        "identity": report["host_reports"][parity.HOST_LINUX_X86_64]["identity"],
        "scorer_version": report["scorer_version"],
        "bundle": report["bundle"],
    }
    with pytest.raises(rounds.RoundError, match="no measured arms"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12159")
def test_append_refuses_host_empty_arms(tmp_path: Path) -> None:
    """[if] a host column has empty arms [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_MACOS_ARM64]["arms"] = {}
    with pytest.raises(rounds.RoundError, match="no measured arms"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12159")
def test_append_refuses_host_scorer_mismatch(tmp_path: Path) -> None:
    """[if] a host scorer disagrees with the round [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_WINDOWS_X86_64]["scorer_version"] = "0.0.1"
    with pytest.raises(rounds.RoundError, match="scorer"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12159")
def test_append_refuses_host_bundle_lane_mismatch(tmp_path: Path) -> None:
    """[if] a host bundle lane disagrees with the round [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_LINUX_X86_64]["bundle"] = {
        **report["bundle"],
        "lane": "key",
    }
    with pytest.raises(rounds.RoundError, match="bundle lane"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12159")
def test_append_refuses_host_arm_role_mismatch(tmp_path: Path) -> None:
    """[if] a host arm map disagrees with the round [then] append refuses, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    report = _report_with_hosts()
    report["host_reports"][parity.HOST_MACOS_ARM64]["arms"] = {
        "beat_this": {"role": "candidate"},
    }
    with pytest.raises(rounds.RoundError, match="arm set"):
        rounds.append_round(log, "beatgrid", report, floor=2, host="fixture")


@pytest.mark.requirement("NATIVE-12159")
def test_run_post_refuses_identity_only_host_reports(bundle: Path, tmp_path: Path, capsys) -> None:
    """[if] merged host JSON omits per-host arms [then] run --post refuses, [else stop]."""
    from apps.analysis_bench import cli

    log = tmp_path / "spec.md"
    log.write_text("# spec\n\n## Experiment log\n")
    report = _report_with_hosts()
    stripped = {
        column: {
            "identity": entry["identity"],
            "scorer_version": entry["scorer_version"],
            "bundle": entry["bundle"],
        }
        for column, entry in report["host_reports"].items()
    }
    host_path = tmp_path / "hosts.json"
    host_path.write_text(json.dumps(stripped), encoding="utf-8")
    run_argv = [
        "run",
        "--lane",
        "beatgrid",
        "--candidate",
        "constant_128",
        "--bundle-dir",
        str(bundle),
        "--workdir",
        str(tmp_path / "arms"),
    ]
    code = cli.main(
        [
            *run_argv,
            "--post",
            "--log-path",
            str(log),
            "--host-reports",
            str(host_path),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err.lower()
    assert "measured arms" in err or "provenance" in err or "arms" in err
    assert log.read_text().endswith("## Experiment log\n")
