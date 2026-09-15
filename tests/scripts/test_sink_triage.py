"""Hermetic tests for scripts/sink_triage.py (issue #2673).

Regression lines:
  - if two messages differ only by path or integer then they share a fingerprint
  - if window count is below 100 and three-run sum is below 10 then no issue
  - if window count is 100+ then threshold fires
  - if dry-run fixture has one new flood and one existing issue then one create
    and one comment command print
  - if kpi last_run is older than 2h then health FAIL for sink-triage freshness
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
    RunResult,
    SinkRecord,
    TriageState,
    aggregate,
    marker_for,
    run_triage,
    threshold_met,
    triage_fingerprint,
)

pytestmark = pytest.mark.requirement("OPS-34")


def _record(
    site: str,
    message: str,
    host: str = "silver",
    *,
    kind: str | None = None,
) -> SinkRecord:
    raw: dict[str, str] = {
        "source_site": site,
        "message": message,
        "host": host,
        "build_sha": "abc123",
    }
    if kind is not None:
        raw["kind"] = kind
    return SinkRecord(
        host=host,
        source_site=site,
        message=message,
        build_sha="abc123",
        error_id="eid-test",
        raw=raw,
    )


_CI_FAILURE_MESSAGE = (
    "CI failed workflow=CI run=34705584177 conclusion=failure "
    "url=https://github.com/maintainer/music-dj-tools/actions/runs/34705584177 "
    "sha=98159af7b5fb6d8ef7fd255a43a5d4386a4530b8"
)


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


def test_build_kind_records_are_excluded_from_triage(tmp_path: Path) -> None:
    assert triage_fingerprint("build:CI", _CI_FAILURE_MESSAGE) == "9e91f071611f"
    records = [
        _record("build:CI", _CI_FAILURE_MESSAGE, host=f"nucbox-wsl-{i}", kind="build")
        for i in range(100)
    ]
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="maintainer/music-dj-tools",
        dry_run=True,
        records_in=records,
    )
    assert result.new_issues == 0
    assert result.comments == 0
    assert result.fingerprints_seen == 0


def test_build_source_site_without_kind_is_excluded(tmp_path: Path) -> None:
    records = [
        _record("build:CI", _CI_FAILURE_MESSAGE, host=f"nucbox-wsl-{i}")
        for i in range(100)
    ]
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="maintainer/music-dj-tools",
        dry_run=True,
        records_in=records,
    )
    assert result.new_issues == 0
    assert result.comments == 0
    assert result.fingerprints_seen == 0


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
