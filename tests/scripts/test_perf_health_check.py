"""Tests for :mod:`scripts.perf_health_check`, the standing perf health check.

Every case here is one of the natural-language acceptance tests written into the module
docstring's mini-PRD. The fixtures are real files in a tmp dir, not mocks: the whole
point of the check is that it reads sinks off a disk, so a fake reader would test
nothing. The one subprocess it makes (``git log -1`` for watchdog checkout freshness) is
pointed at a real throwaway repository for the same reason.

The checks take their sink paths as arguments, so no module global is patched here.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import perf_health_check as mod

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ----- fixtures ---------------------------------------------------------------------


def _row(**overrides) -> str:
    record = {
        "event_id": "abc123",
        "received_at": "2026-08-30T10:00:00.000Z",
        "kind": "ui-error",
        "message": "something benign happened",
        "context": {"source": "toast", "toast_id": 1},
    }
    record.update(overrides)
    return json.dumps(record)


def _write_log(directory: Path, day: str, rows: list[str], prefix: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{prefix or mod.CLIENT_ERROR_PREFIX}-{day}.log"
    path = directory / name
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _age_file(path: Path, hours: float) -> None:
    stamp = (NOW - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


@pytest.fixture
def log_root(tmp_path: Path) -> Path:
    return tmp_path / "logs"


# ----- R1 client-error scan ---------------------------------------------------------


def test_signalsmith_rows_counted_per_day_with_pointer(log_root: Path):
    """If a day's log holds 2 Signalsmith timeout rows
    then the day count is 2, against that day's total rows, with the xrun pointer,
    or broken.
    """
    _write_log(
        log_root,
        "2026-08-30",
        [
            _row(message="Signalsmith schedule timed out after 5000ms"),
            _row(message="Signalsmith processor creation timed out after 15000ms"),
            _row(),
        ],
    )
    result = mod.check_client_errors((log_root,), 7, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "findings"
    assert "0 deck-load failures and 2 Signalsmith timeouts" in result.detail
    assert "3 rows across 1 daily log" in result.detail
    assert result.breakdown == ["2026-08-30: 0 deck-load, 2 signalsmith in 3 rows"]
    assert len(result.findings) == 2
    for finding in result.findings:
        assert finding.signature == "signalsmith-timeout"
        assert "worklet-ack p95" in finding.pointer
        assert "xrun" in finding.pointer


def test_deck_load_failure_renders_the_heaviest_stage_first(log_root: Path):
    """If a deck-load failure row carries stage_* context
    then the heaviest stage leads the rendered evidence context, or broken.
    """
    _write_log(
        log_root,
        "2026-08-30",
        [
            _row(
                message="Deck 1 load failed - Error: decode failed",
                context={
                    "source": "deck-load",
                    "deck": 1,
                    "stage_fetchAudio": 120,
                    "stage_decodeMix": 980,
                    "stage_failedAt": 3,
                },
            )
        ],
    )
    result = mod.check_client_errors((log_root,), 7, NOW)

    assert "1 deck-load failures and 0 Signalsmith timeouts" in result.detail
    finding = result.findings[0]
    assert finding.signature == "deck-load-context"
    assert finding.context.startswith("stage_decodeMix=980 stage_fetchAudio=120")
    assert "stage_*" in finding.pointer
    assert mod.QUEUE_DOC in finding.pointer


def test_pre_539_deck_load_messages_still_match(log_root: Path):
    """If a deck-load failure predates the stage context
    then the message signature still counts it, or broken.
    """
    _write_log(
        log_root,
        "2026-08-30",
        [
            _row(message="cannot load: audio file missing on disk (broken link)"),
            _row(message="drop load failed: unsupported container"),
            _row(message="Performance command failed - Error: load: deck 4 must be stopped"),
        ],
    )
    result = mod.check_client_errors((log_root,), 7, NOW)

    assert "3 deck-load failures" in result.detail
    signatures = {finding.signature for finding in result.findings}
    assert signatures == {"audio-missing", "drop-load-failed", "load-precondition"}


def test_absent_log_roots_warn_and_name_every_path(tmp_path: Path):
    """If no client-error log root exists
    then [WARN] naming every expected path, never a crash and never a silent pass,
    or broken.
    """
    missing = (tmp_path / "nope-a", tmp_path / "nope-b")
    result = mod.check_client_errors(missing, 7, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "sink-absent"
    assert "0 rows were readable" in result.detail
    for path in missing:
        assert str(path) in result.remediation


def test_malformed_row_is_counted_and_the_scan_continues(log_root: Path):
    """If a log line is not valid JSON
    then it is counted with its file and line number and the rest of the file is still
    scanned, or broken.
    """
    log_root.mkdir(parents=True)
    path = log_root / f"{mod.CLIENT_ERROR_PREFIX}-2026-08-30.log"
    path.write_text(
        "\n".join(
            [
                _row(),
                "{not json at all",
                "[1, 2, 3]",
                _row(message="Signalsmith schedule timed out after 5000ms"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = mod.check_client_errors((log_root,), 7, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert "2 malformed rows" in result.detail
    assert "4 rows across 1 daily log" in result.detail
    assert f"{path}:2" in result.remediation
    assert f"{path}:3" in result.remediation
    assert len(result.findings) == 1, "the row after the malformed ones must still be scanned"


def test_logs_outside_the_window_are_not_scanned(log_root: Path):
    """If the only daily log predates the window
    then [WARN] no-logs-in-window naming the root, or broken.
    """
    _write_log(log_root, "2026-08-01", [_row(message="Signalsmith schedule timed out")])
    result = mod.check_client_errors((log_root,), 7, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "no-logs-in-window"
    assert "0 daily logs inside the last 7 days" in result.detail
    assert str(log_root) in result.remediation


def test_both_default_roots_are_scanned_together(tmp_path: Path):
    """If two roots each hold a log for the same day
    then their rows are counted into one day bucket, or broken.
    """
    engine = tmp_path / "engine-logs"
    legacy = tmp_path / "legacy-logs"
    _write_log(engine, "2026-08-30", [_row(message="Signalsmith schedule timed out")])
    _write_log(legacy, "2026-08-30", [_row(message="Deck 2 load failed - Error: boom")])
    result = mod.check_client_errors((engine, legacy), 7, NOW)

    assert result.breakdown == ["2026-08-30: 1 deck-load, 1 signalsmith in 2 rows"]
    assert "2 rows across 2 daily logs" in result.detail


def test_a_clean_window_is_ok_and_still_quotes_its_denominator(log_root: Path):
    """If a scanned window holds no findings
    then [OK] still quoting rows and daily logs, or broken.
    """
    _write_log(log_root, "2026-08-30", [_row(), _row()])
    result = mod.check_client_errors((log_root,), 7, NOW)

    assert result.severity == mod.SEVERITY_OK
    assert result.classification == "healthy"
    assert "2 rows across 1 daily log" in result.detail


# ----- R2 iteration metrics ---------------------------------------------------------


def _metric_line(moment: datetime) -> str:
    return json.dumps(
        {"ts": moment.strftime("%Y-%m-%dT%H:%M:%SZ"), "step": "quality", "seconds": 12.0}
    )


def test_absent_iteration_store_warns_and_names_the_path(tmp_path: Path):
    """If the metrics store is absent then [WARN] naming the path, or broken."""
    store = tmp_path / "metrics.jsonl"
    result = mod.check_iteration_metrics(store, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "sink-absent"
    assert str(store) in result.detail


def test_iteration_store_with_no_recent_rows_carries_the_burst_caveat(tmp_path: Path):
    """If the store holds 0 rows inside the 48h window
    then [WARN] carrying the JOB_FETCH_RUN_COUNT burst caveat, or broken.
    """
    store = tmp_path / "metrics.jsonl"
    store.write_text(_metric_line(NOW - timedelta(days=10)) + "\n", encoding="utf-8")
    _age_file(store, 240)
    result = mod.check_iteration_metrics(store, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "no-recent-rows"
    assert "0 rows appended in the last 48h of 1 rows in the store" in result.detail
    assert "JOB_FETCH_RUN_COUNT=10" in result.remediation


def test_iteration_store_with_recent_rows_is_ok_with_both_numbers(tmp_path: Path):
    """If the store holds rows inside the window
    then [OK] quoting rows-in-window over total rows, or broken.
    """
    store = tmp_path / "metrics.jsonl"
    store.write_text(
        "\n".join(
            [
                _metric_line(NOW - timedelta(days=10)),
                _metric_line(NOW - timedelta(hours=3)),
                _metric_line(NOW - timedelta(hours=1)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _age_file(store, 1)
    result = mod.check_iteration_metrics(store, NOW)

    assert result.severity == mod.SEVERITY_OK
    assert "2 rows appended in the last 48h of 3 rows in the store" in result.detail
    assert "1.0h old" in result.detail


def test_iteration_store_malformed_row_is_reported_not_crashed(tmp_path: Path):
    """If the store holds a malformed line
    then it is counted and reported with its line number, or broken.
    """
    store = tmp_path / "metrics.jsonl"
    store.write_text(
        "\n".join([_metric_line(NOW - timedelta(hours=1)), "{broken", '{"step": "no-ts"}'])
        + "\n",
        encoding="utf-8",
    )
    _age_file(store, 1)
    result = mod.check_iteration_metrics(store, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "malformed-rows"
    assert "2 malformed rows" in result.detail
    assert "line 2" in result.remediation
    assert "line 3 (no ts field)" in result.remediation


# ----- R3 CI-health watchdog --------------------------------------------------------


def _watchdog_log(tmp_path: Path, moments: list[datetime]) -> Path:
    path = tmp_path / "mdt-ci-health.log"
    lines = []
    for moment in moments:
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{stamp} [OK] ci-health: starting check for the music-dj-tools repo")
        lines.append(f"{stamp} [OK] ci-health: all checks passed, no alert needed")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _git_repo(tmp_path: Path, *, commit_age_days: float) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    committed = (NOW - timedelta(days=commit_age_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": committed,
        "GIT_COMMITTER_DATE": committed,
    }
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, timeout=30)
    return repo


def test_absent_watchdog_log_warns(tmp_path: Path):
    """If the watchdog log is absent then [WARN] naming the path, or broken."""
    result = mod.check_ci_health_watchdog(tmp_path / "gone.log", tmp_path / "repo", NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "sink-absent"
    assert "0 runs were readable" in result.detail


def test_stale_watchdog_run_warns_with_the_expected_schedule(tmp_path: Path):
    """If the newest log timestamp is older than the max run age
    then [WARN] naming the age and the 4-hourly schedule, or broken.
    """
    log = _watchdog_log(tmp_path, [NOW - timedelta(hours=30)])
    result = mod.check_ci_health_watchdog(log, tmp_path / "no-repo", NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "stale-run"
    assert "30.0h old" in result.detail
    assert "4-hourly" in result.remediation


def test_fresh_watchdog_run_reports_the_verdict_line_not_the_start_line(tmp_path: Path):
    """If the newest run is fresh
    then the reported status is the verdict line, never the 'starting check' line,
    or broken.
    """
    log = _watchdog_log(tmp_path, [NOW - timedelta(hours=2)])
    repo = _git_repo(tmp_path, commit_age_days=1)
    result = mod.check_ci_health_watchdog(log, repo, NOW)

    assert result.severity == mod.SEVERITY_OK
    assert "all checks passed" in result.detail
    assert "starting check" not in result.detail
    assert "checkout last commit 1.0d old" in result.detail


def test_stale_watchdog_checkout_warns_about_the_failing_self_update(tmp_path: Path):
    """If the watchdog checkout's last commit is older than 7 days
    then [WARN] saying the PR #538 self-update pull is failing, or broken.
    """
    log = _watchdog_log(tmp_path, [NOW - timedelta(hours=2)])
    repo = _git_repo(tmp_path, commit_age_days=30)
    result = mod.check_ci_health_watchdog(log, repo, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "stale-checkout"
    assert "#538" in result.remediation


def test_missing_watchdog_checkout_is_named_not_assumed(tmp_path: Path):
    """If there is no watchdog checkout
    then [WARN] checkout-unknown naming the missing path, never a silent OK, or broken.
    """
    log = _watchdog_log(tmp_path, [NOW - timedelta(hours=2)])
    result = mod.check_ci_health_watchdog(log, tmp_path / "no-repo", NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "checkout-unknown"
    assert str(tmp_path / "no-repo") in result.remediation


def test_watchdog_log_without_a_parseable_timestamp_warns(tmp_path: Path):
    """If the log exists but holds no parseable timestamp
    then [WARN] naming the line count it read, or broken.
    """
    log = tmp_path / "mdt-ci-health.log"
    log.write_text("garbage\nmore garbage\n", encoding="utf-8")
    result = mod.check_ci_health_watchdog(log, tmp_path / "no-repo", NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "unparseable-log"
    assert "2 log lines" in result.detail


# ----- R4 diagnostics probe ---------------------------------------------------------


def test_absent_probe_dir_points_at_the_install_runbook(tmp_path: Path):
    """If the probe output directory is absent
    then [WARN] 'probe not installed' pointing at the runbook, or broken.
    """
    result = mod.check_diagnostics_probe(tmp_path / "performance", NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "not-installed"
    assert "probe not installed" in result.detail
    assert mod.PROBE_INSTALL_DOC in result.remediation


def test_probe_dir_with_no_samples_warns(tmp_path: Path):
    """If the probe directory exists but holds no samples then [WARN], or broken."""
    probe_dir = tmp_path / "performance"
    probe_dir.mkdir()
    result = mod.check_diagnostics_probe(probe_dir, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "no-samples"
    assert "0 *.jsonl samples" in result.detail


def test_fresh_probe_sample_is_ok(tmp_path: Path):
    """If the newest probe sample is fresh then [OK] naming the file count, or broken."""
    probe_dir = tmp_path / "performance"
    probe_dir.mkdir()
    sample = probe_dir / "2026-08-31.jsonl"
    sample.write_text("{}\n", encoding="utf-8")
    _age_file(sample, 2)
    result = mod.check_diagnostics_probe(probe_dir, NOW)

    assert result.severity == mod.SEVERITY_OK
    assert "newest of 1 sample files is 2.0h old" in result.detail


def test_stale_probe_sample_says_the_probe_stopped(tmp_path: Path):
    """If the newest sample is older than the max age
    then [WARN] saying a 15s sampler that went quiet has stopped, or broken.
    """
    probe_dir = tmp_path / "performance"
    probe_dir.mkdir()
    sample = probe_dir / "2026-08-20.jsonl"
    sample.write_text("{}\n", encoding="utf-8")
    _age_file(sample, 120)
    result = mod.check_diagnostics_probe(probe_dir, NOW)

    assert result.severity == mod.SEVERITY_WARN
    assert result.classification == "stale-samples"
    assert "every 15s" in result.remediation


# ----- R5 machine-readable output ---------------------------------------------------


def test_json_output_parses_and_carries_every_denominator(capsys):
    """If --json is passed
    then stdout parses as JSON and every check carries its window and findings,
    or broken.
    """
    exit_code = mod.main(["--json", "--days", "1"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code in (mod.EXIT_OK, mod.EXIT_ERROR)
    assert set(payload) >= {"checked_at", "window_days", "exit_code", "ok", "checks"}
    assert payload["window_days"] == 1
    assert len(payload["checks"]) == 5
    for check in payload["checks"]:
        assert check["window"], f"{check['check']} has no window"
        assert check["severity"] in mod.SEVERITIES
        assert isinstance(check["findings"], list)


def test_text_output_leads_with_the_outcome_and_uses_bracket_markers(capsys):
    """If the text renderer runs
    then the first line is the headline verdict with a bracket marker, or broken.
    """
    exit_code = mod.main(["--days", "1"])
    lines = capsys.readouterr().out.splitlines()

    assert exit_code in (mod.EXIT_OK, mod.EXIT_ERROR)
    assert lines[0].startswith(("[OK] perf-health:", "[WARN] perf-health:", "[ERROR] perf-health:"))
    assert "5 checks over the last 1 day" in lines[0]


def test_days_must_be_at_least_one():
    """If --days is 0 then argparse exits nonzero rather than scanning nothing, or broken."""
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--days", "0"])
    assert excinfo.value.code != 0


# ----- R6 unreadable sinks ----------------------------------------------------------


def test_a_directory_where_a_jsonl_belongs_is_an_error_not_a_traceback(tmp_path: Path):
    """If a JSONL sink path is a directory then [ERROR] naming the path, or broken."""
    directory = tmp_path / "metrics.jsonl"
    directory.mkdir()
    result = mod._guarded(
        "iteration-metrics", lambda: mod.check_iteration_metrics(directory, NOW)
    )

    assert result.severity == mod.SEVERITY_ERROR
    assert result.classification == "unreadable-sink"
    assert str(directory) in result.detail


def test_an_unreadable_sink_fails_the_exit_code_and_spares_the_others(tmp_path: Path):
    """If one sink is unreadable
    then only that check is [ERROR], the exit code is 1, and the rest still ran,
    or broken.
    """
    directory = tmp_path / "metrics.jsonl"
    directory.mkdir()
    results = [
        mod.check_diagnostics_probe(tmp_path / "performance", NOW),
        mod._guarded("iteration-metrics", lambda: mod.check_iteration_metrics(directory, NOW)),
    ]

    assert mod.resolve_exit_code(results) == mod.EXIT_ERROR
    assert results[0].severity == mod.SEVERITY_WARN
    assert results[1].severity == mod.SEVERITY_ERROR


def test_main_exits_one_when_a_sink_is_unreadable(tmp_path: Path, monkeypatch, capsys):
    """If a sink exists but cannot be read
    then the CLI exits 1 with an [ERROR] line and the other checks still ran, or broken.

    collect_results is the only function that knows the machine layout, so pointing one
    of its constants at a tmp path is how the CLI-level exit code is exercised.
    """
    directory = tmp_path / "metrics.jsonl"
    directory.mkdir()
    monkeypatch.setattr(mod, "ITERATION_METRICS_PATH", directory)

    exit_code = mod.main(["--days", "1"])
    out = capsys.readouterr().out

    assert exit_code == mod.EXIT_ERROR
    assert "[ERROR] iteration-metrics: sink exists but could not be read" in out
    assert out.splitlines()[0].startswith("[ERROR] perf-health:")
    assert "1 error," in out.splitlines()[0]


def test_a_warn_only_run_still_exits_zero(tmp_path: Path):
    """If every finding is a [WARN] then the exit code stays 0, or broken."""
    results = [mod.check_diagnostics_probe(tmp_path / "performance", NOW)]

    assert results[0].severity == mod.SEVERITY_WARN
    assert mod.resolve_exit_code(results) == mod.EXIT_OK


# ----- headline ---------------------------------------------------------------------


def test_headline_counts_every_severity(tmp_path: Path):
    """If the run mixes severities then the headline counts each one, or broken."""
    results = [
        mod.check_diagnostics_probe(tmp_path / "performance", NOW),
        mod.check_client_perf_sink((tmp_path / "logs",), 7, NOW),
    ]
    headline = mod._headline(results, 7)

    assert headline.startswith("[WARN] perf-health: 2 checks over the last 7 days")
    assert "0 error, 1 warn, 0 ok, 1 info" in headline
