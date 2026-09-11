"""Unit tests for acid-test squeeze report JSON validation and comparison."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.squeeze_report import (
    LIBRARY_SCALE_MIN_TRACKS,
    compare_reports,
    main,
    resolve_library_scale,
    validate_capture,
    write_report,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "perf" / "squeeze"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _valid_capture(*, app_build_dirty: bool = False, xrun_boundary_ack: bool = True) -> dict:
    return {
        "deck_load_ms": 100.0,
        "xruns": 0,
        "ui_latency_ms": 20.0,
        "app_build_sha": SHA,
        "app_build_dirty": app_build_dirty,
        "frontend_build_sha": SHA,
        "frontend_build_dirty": False,
        "xrun_session_id": "session-1",
        "xrun_boundary_ack": xrun_boundary_ack,
    }


def _write_capture(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_four_captures(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "baseline": tmp_path / "baseline.json",
        "during": tmp_path / "during.json",
        "pressure-end": tmp_path / "pressure-end.json",
        "after": tmp_path / "after.json",
    }
    for phase, path in paths.items():
        payload = _valid_capture()
        if phase == "during":
            payload["xrun_boundary_ack"] = False
        if phase == "after":
            payload["xrun_boundary_ack"] = False
        if phase in ("during", "pressure-end", "after"):
            payload["xruns"] = 1
        _write_capture(path, payload)
    return paths


@pytest.mark.requirement("PERFMODE-06")
def test_dirty_app_build_is_rejected() -> None:
    """If app_build_dirty is true, then validate_capture rejects it."""
    payload = _valid_capture(app_build_dirty=True)
    with pytest.raises(SystemExit):
        validate_capture(payload, "baseline")


@pytest.mark.requirement("PERFMODE-06")
def test_write_report_rejects_dirty_app_build(tmp_path: Path) -> None:
    """If app_build_dirty is true, then write_report does not write a report file."""
    paths = _write_four_captures(tmp_path)
    dirty = _valid_capture(app_build_dirty=True)
    _write_capture(paths["baseline"], dirty)
    output = tmp_path / "report.json"
    with pytest.raises(SystemExit):
        write_report(
            baseline_path=paths["baseline"],
            during_path=paths["during"],
            pressure_end_path=paths["pressure-end"],
            after_path=paths["after"],
            output_path=output,
            machine_tag="silver-squeeze-cpu-80",
            hostname="silver",
            kind="cpu",
            level="80",
            duration_seconds=60,
            harness_source_sha=SHA,
            capture_implementation=f"/tmp/capture.sh@{SHA}",
        )
    assert not output.exists()


@pytest.mark.requirement("PERFMODE-06")
def test_finished_report_contains_required_kpis(tmp_path: Path) -> None:
    """If four valid captures exist, then write_report emits the required schema."""
    paths = _write_four_captures(tmp_path)
    output = tmp_path / "report.json"
    report = write_report(
        baseline_path=paths["baseline"],
        during_path=paths["during"],
        pressure_end_path=paths["pressure-end"],
        after_path=paths["after"],
        output_path=output,
        machine_tag="silver-squeeze-cpu-80",
        hostname="silver",
        kind="cpu",
        level="80",
        duration_seconds=60,
        harness_source_sha=SHA,
        capture_implementation=f"/tmp/capture.sh@{SHA}",
    )
    assert output.exists()
    assert report["measured_app_build_sha"] == SHA
    assert report["machine_tag"] == "silver-squeeze-cpu-80"
    assert report["hostname"] == "silver"
    for phase in ("baseline", "during", "pressure-end", "after"):
        capture = report["captures"][phase]
        assert "deck_load_ms" in capture
        assert "xruns" in capture
        assert "ui_latency_ms" in capture
        assert capture["app_build_sha"] == SHA


@pytest.mark.requirement("PERFMODE-06")
def test_missing_kpi_is_not_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """If deck_load_ms is missing or null, then validation rejects it without substituting 0."""
    for payload in (
        {k: v for k, v in _valid_capture().items() if k != "deck_load_ms"},
        {**_valid_capture(), "deck_load_ms": None},
    ):
        with pytest.raises(SystemExit):
            validate_capture(payload, "during")
        assert "deck_load_ms" in capsys.readouterr().err
        assert payload.get("deck_load_ms") != 0

    for name in ("xruns", "ui_latency_ms"):
        payload = {k: v for k, v in _valid_capture().items() if k != name}
        with pytest.raises(SystemExit):
            validate_capture(payload, "during")
        assert name in capsys.readouterr().err


@pytest.mark.requirement("PERFMODE-06")
def test_compare_two_hosts_with_same_schema(tmp_path: Path) -> None:
    """If two reports differ only by machine id, then compare_reports succeeds."""
    paths = _write_four_captures(tmp_path)
    report_a_path = tmp_path / "silver.json"
    report_b_path = tmp_path / "air.json"
    write_report(
        baseline_path=paths["baseline"],
        during_path=paths["during"],
        pressure_end_path=paths["pressure-end"],
        after_path=paths["after"],
        output_path=report_a_path,
        machine_tag="silver-squeeze-cpu-80",
        hostname="silver",
        kind="cpu",
        level="80",
        duration_seconds=60,
        harness_source_sha=SHA,
        capture_implementation=f"/tmp/capture.sh@{SHA}",
    )
    write_report(
        baseline_path=paths["baseline"],
        during_path=paths["during"],
        pressure_end_path=paths["pressure-end"],
        after_path=paths["after"],
        output_path=report_b_path,
        machine_tag="air-squeeze-cpu-80",
        hostname="air",
        kind="cpu",
        level="80",
        duration_seconds=60,
        harness_source_sha=SHA,
        capture_implementation=f"/tmp/capture.sh@{SHA}",
    )
    report_a = json.loads(report_a_path.read_text(encoding="utf-8"))
    report_b = json.loads(report_b_path.read_text(encoding="utf-8"))
    compare_reports(report_a, report_b)


@pytest.mark.requirement("PERFMODE-06")
def test_compare_rejects_missing_required_kpi(capsys: pytest.CaptureFixture[str]) -> None:
    """If a required KPI is missing, then compare rejects rather than filling zero."""
    complete = json.loads((FIXTURES / "silver-report.json").read_text(encoding="utf-8"))
    broken = json.loads((FIXTURES / "air-report-missing-ui-latency.json").read_text(encoding="utf-8"))
    with pytest.raises(SystemExit):
        compare_reports(complete, broken)
    err = capsys.readouterr().err
    assert "ui_latency_ms" in err
    assert "during" in err


@pytest.mark.requirement("PERFMODE-06")
def test_library_scale_absent_names_gap() -> None:
    """If no library-scale fixture is present, then track_count is null and reason names the gap."""
    resolved = resolve_library_scale(None)
    assert resolved["present"] is False
    assert resolved["track_count"] is None
    assert resolved["track_count"] != 0
    assert "library-scale fixture is absent" in resolved["reason"]
    assert "2-track" in resolved["reason"]

    missing = resolve_library_scale(Path("/tmp/does-not-exist-fixture"))
    assert missing["present"] is False
    assert missing["track_count"] is None


@pytest.mark.requirement("PERFMODE-06")
def test_two_track_fixture_is_not_library_scale(tmp_path: Path) -> None:
    """If track_count is 2, then it is not labeled library-scale."""
    fixture = tmp_path / "two-track.json"
    fixture.write_text(json.dumps({"track_count": 2}), encoding="utf-8")
    resolved = resolve_library_scale(fixture)
    assert resolved["present"] is False
    assert resolved["track_count"] == 2
    assert "2-track" in resolved["reason"]

    paths = _write_four_captures(tmp_path)
    output = tmp_path / "report.json"
    report = write_report(
        baseline_path=paths["baseline"],
        during_path=paths["during"],
        pressure_end_path=paths["pressure-end"],
        after_path=paths["after"],
        output_path=output,
        machine_tag="silver-squeeze-cpu-80",
        hostname="silver",
        kind="cpu",
        level="80",
        duration_seconds=60,
        harness_source_sha=SHA,
        capture_implementation=f"/tmp/capture.sh@{SHA}",
        library_scale_fixture=fixture,
    )
    assert report["library_scale"]["present"] is False


@pytest.mark.requirement("PERFMODE-06")
def test_qualifying_library_scale_fixture(tmp_path: Path) -> None:
    """If track_count is at least 1000, then library_scale.present is true."""
    fixture = tmp_path / "library-scale.json"
    fixture.write_text(json.dumps({"track_count": LIBRARY_SCALE_MIN_TRACKS}), encoding="utf-8")
    resolved = resolve_library_scale(fixture)
    assert resolved["present"] is True
    assert resolved["track_count"] == LIBRARY_SCALE_MIN_TRACKS


@pytest.mark.requirement("PERFMODE-06")
def test_cli_compare_two_reports(tmp_path: Path) -> None:
    """If compare is invoked on two valid reports, then it exits successfully."""
    paths = _write_four_captures(tmp_path)
    report_a = tmp_path / "a.json"
    report_b = tmp_path / "b.json"
    write_report(
        baseline_path=paths["baseline"],
        during_path=paths["during"],
        pressure_end_path=paths["pressure-end"],
        after_path=paths["after"],
        output_path=report_a,
        machine_tag="silver-squeeze-cpu-80",
        hostname="silver",
        kind="cpu",
        level="80",
        duration_seconds=60,
        harness_source_sha=SHA,
        capture_implementation=f"/tmp/capture.sh@{SHA}",
    )
    write_report(
        baseline_path=paths["baseline"],
        during_path=paths["during"],
        pressure_end_path=paths["pressure-end"],
        after_path=paths["after"],
        output_path=report_b,
        machine_tag="air-squeeze-cpu-80",
        hostname="air",
        kind="cpu",
        level="80",
        duration_seconds=60,
        harness_source_sha=SHA,
        capture_implementation=f"/tmp/capture.sh@{SHA}",
    )
    main(["compare", str(report_a), str(report_b)])


@pytest.mark.requirement("PERFMODE-06")
def test_library_scale_cli_prints_named_gap() -> None:
    """If library-scale is invoked without a fixture, then it names the gap on stderr."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.perf.squeeze_report", "library-scale"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["present"] is False
    assert payload["track_count"] is None
    assert "library-scale fixture is absent" in result.stderr
