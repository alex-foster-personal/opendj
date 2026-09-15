"""Hermetic tests for scripts/sink_triage.py (issue #2673).

Regression lines:
  - if two messages differ only by path or integer then they share a fingerprint
  - if window count is below 100 and three-run sum is below 10 then no issue
  - if window count is 100+ then threshold fires
  - if dry-run fixture has one new flood and one existing issue then one create
    and one comment command print
  - if kpi last_run is older than 2h then health FAIL for sink-triage freshness

[if] sink fingerprints and thresholds fire [then] triage creates or comments on issues, [else stop].
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.sink_triage import (
    FingerprintStats,
    HostSource,
    RunResult,
    SinkRecord,
    TriageState,
    aggregate,
    collect_host_records,
    marker_for,
    parse_record,
    run_triage,
    threshold_met,
    triage_fingerprint,
)

pytestmark = pytest.mark.requirement("OPS-34")


def _record(site: str, message: str, host: str = "silver") -> SinkRecord:
    return SinkRecord(
        host=host,
        source_site=site,
        message=message,
        build_sha="abc123",
        error_id="eid-test",
        raw={"source_site": site, "message": message, "host": host, "build_sha": "abc123"},
    )


def _build_e2e_message(run_id: int = 34705843143) -> str:
    return (
        f"CI failed workflow=E2E run={run_id} conclusion=failure "
        f"url=https://github.com/maintainer/music-dj-tools/actions/runs/{run_id} "
        f"sha=921b0489fa060e5945b8302285b2a2a872b4ab40"
    )


def _build_e2e_record(host: str = "nucbox-wsl-8", run_id: int = 34705843143) -> SinkRecord:
    message = _build_e2e_message(run_id)
    return SinkRecord(
        host=host,
        source_site="build:E2E",
        message=message,
        build_sha="921b0489fa060e5945b8302285b2a2a872b4ab40",
        error_id="eid-21f55ba9a63a",
        raw={
            "build_sha": "921b0489fa060e5945b8302285b2a2a872b4ab40",
            "error_id": "eid-21f55ba9a63a",
            "host": host,
            "kind": "build",
            "message": message,
            "source_site": "build:E2E",
        },
    )


def test_parse_record_skips_kind_build() -> None:
    line = json.dumps(
        {
            "kind": "build",
            "source_site": "build:E2E",
            "message": _build_e2e_message(),
            "host": "nucbox-wsl-8",
        }
    )
    assert parse_record(line, "nucbox") is None


def test_parse_record_skips_build_source_site_without_kind() -> None:
    line = json.dumps(
        {
            "source_site": "build:E2E",
            "message": _build_e2e_message(),
            "host": "nucbox-wsl-8",
        }
    )
    assert parse_record(line, "nucbox") is None


def test_build_e2e_fingerprint_anchor() -> None:
    assert triage_fingerprint("build:E2E", _build_e2e_message()) == "878d8ed91bfc"


def _collect_from_jsonl(tmp_path: Path, lines: list[dict[str, object]], name: str = "nucbox") -> list[SinkRecord]:
    sink = tmp_path / f"{name}-sink.jsonl"
    sink.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    state = TriageState(offsets={}, run_history={}, last_comment={})
    source = HostSource(name=name, mode="local", sink_path=str(sink))
    return collect_host_records(source, state)


def _gh_no_open_issues(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, "", "")


def test_build_e2e_flood_does_not_file_issues(tmp_path: Path) -> None:
    lines = [
        {
            "build_sha": "921b0489fa060e5945b8302285b2a2a872b4ab40",
            "error_id": "eid-21f55ba9a63a",
            "host": f"nucbox-wsl-{i % 30}",
            "kind": "build",
            "message": _build_e2e_message(34705843143 + i),
            "source_site": "build:E2E",
        }
        for i in range(225)
    ]
    records = _collect_from_jsonl(tmp_path, lines)
    assert records == []
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="maintainer/music-dj-tools",
        dry_run=True,
        records_in=records,
        gh_run=_gh_no_open_issues,
    )
    assert result.new_issues == 0
    assert result.comments == 0
    assert result.commands == []


def test_engine_flood_still_files_when_build_rows_present(tmp_path: Path) -> None:
    engine_lines = [
        {
            "source_site": "engine:flood",
            "message": "disk write failed on path /tmp/x",
            "host": "silver",
            "build_sha": "abc123",
        }
    ] * 100
    build_lines = [
        {
            "build_sha": "921b0489fa060e5945b8302285b2a2a872b4ab40",
            "error_id": "eid-21f55ba9a63a",
            "host": f"nucbox-wsl-{i % 30}",
            "kind": "build",
            "message": _build_e2e_message(34705843143 + i),
            "source_site": "build:E2E",
        }
        for i in range(225)
    ]
    records = _collect_from_jsonl(tmp_path, engine_lines + build_lines)
    assert len(records) == 100
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="maintainer/music-dj-tools",
        dry_run=True,
        records_in=records,
        gh_run=_gh_no_open_issues,
        sb_run=lambda _args: "- [ ] stop the flood\n",
    )
    assert result.new_issues == 1
    assert result.comments == 0
    assert len(result.commands) == 1
    assert "issue create" in result.commands[0]


def test_paths_and_numbers_share_a_fingerprint() -> None:
    a = triage_fingerprint("engine:runner:run", "open failed /home/foo/a/17")
    b = triage_fingerprint("engine:runner:run", "open failed /var/tmp/b/99")
    assert a == b


def test_different_source_sites_differ() -> None:
    message = "job 17 failed"
    a = triage_fingerprint("engine:a", message)
    b = triage_fingerprint("engine:b", message)
    assert a != b


def test_threshold_window_count() -> None:
    stats = FingerprintStats(fingerprint="fp1", count=100)
    assert threshold_met(stats, []) is True
    stats.count = 1
    assert threshold_met(stats, [1, 1]) is False


def test_threshold_three_run_sum() -> None:
    stats = FingerprintStats(fingerprint="fp1", count=4)
    assert threshold_met(stats, [3, 3]) is True
    stats.count = 2
    assert threshold_met(stats, [3, 3]) is False


def test_dry_run_fixture_emits_one_create_and_one_comment(tmp_path: Path) -> None:
    fp_new = triage_fingerprint("engine:flood", "disk write failed on path /tmp/x")
    fp_low = triage_fingerprint("engine:quiet", "one-off")
    fp_existing = triage_fingerprint("engine:known", "repeat failure")
    records = [_record("engine:flood", "disk write failed on path /tmp/x")] * 100
    records.append(_record("engine:quiet", "one-off"))
    records.extend([_record("engine:known", "repeat failure")] * 100)

    def gh_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if args[:2] == ["issue", "list"] and marker_for(fp_existing) in joined:
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="maintainer/music-dj-tools",
        dry_run=True,
        records_in=records,
        gh_run=gh_run,
        sb_run=lambda _args: "- [ ] stop the flood\n",
    )
    assert result.new_issues == 1
    assert result.comments == 1
    assert len(result.commands) == 2
    assert any("issue create" in cmd for cmd in result.commands)
    assert any("issue comment 4242" in cmd for cmd in result.commands)
    assert fp_low not in " ".join(result.commands)


def test_kpi_health_fails_when_last_run_stale(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    kpi = repo / "ops" / "fleet" / "kpi.sh"
    fixture_root = tmp_path / "fixture"
    shutil.copytree(repo / "tests/fixtures/fleet-kpi", fixture_root)
    fixture = fixture_root / "jobs"
    now = int(datetime.now(UTC).timestamp())
    os.utime(fixture / "logs" / "tick-gate.log", (now - 120, now - 120))
    stale = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (fixture / "state" / "sink-triage-kpi.json").write_text(
        json.dumps({"last_run": stale, "new_issues": 0, "fingerprints": 1}) + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / ".profile").write_text("export CLAUDE_CODE_OAUTH_TOKEN=x\n")
    env = {
        "HOME": str(home),
        "KPI_JOBS_DIR": str(fixture),
        "KPI_GH_FIXTURES_DIR": str(fixture_root / "gh"),
        "KPI_PROBES_DIR": str(fixture_root / "probes"),
        "KPI_NOW_UNIX": str(now),
        "KPI_SKIP_REVIEW_COST": "1",
    }
    proc = subprocess.run(
        ["bash", str(kpi), "1"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert "sink-triage last_run=" in proc.stdout
    assert "health FAIL sink-triage last run within" in proc.stdout


def test_aggregate_counts_hosts_and_examples() -> None:
    records = [
        _record("engine:a", "boom 1", "silver"),
        _record("engine:a", "boom 2", "air"),
    ]
    grouped = aggregate(records)
    assert len(grouped) == 1
    stats = next(iter(grouped.values()))
    assert stats.count == 2
    assert stats.hosts == {"silver", "air"}
    assert len(stats.examples) == 2
