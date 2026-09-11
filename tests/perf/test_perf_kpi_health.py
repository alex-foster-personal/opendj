"""Health probe: two-timeout restart decision and JSONL logging."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.perf.perf_kpi_health import HealthConfig, run_health_tick


def test_first_timeout_logs_error_without_restart(tmp_path: Path) -> None:
    """If one health probe times out then it logs an error row and does not restart."""
    log = tmp_path / "health.jsonl"
    state = tmp_path / "state.json"
    restarted: list[str] = []

    code = run_health_tick(
        HealthConfig(
            health_url="http://127.0.0.1:1/api/v1/health",
            state_file=state,
            health_log=log,
            timeout_s=0.05,
            failure_threshold=2,
            restart_command=("false",),
            engine_log_path=tmp_path / "missing.log",
        ),
        probe=lambda _url, _timeout: "connection refused",
        restart=lambda _cmd: restarted.append("yes") or (True, "ok"),
        tail_log=lambda _path, _lines: [],
    )

    assert code == 1
    assert restarted == []
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["event"] == "unhealthy"
    assert row["error"]
    assert json.loads(state.read_text(encoding="utf-8"))["consecutive_failures"] == 1


def test_second_timeout_restarts_and_logs_tail(tmp_path: Path) -> None:
    """If two consecutive probes fail then restart runs once and the tail is logged."""
    log = tmp_path / "health.jsonl"
    state = tmp_path / "state.json"
    engine_log = tmp_path / "engine.log"
    engine_log.write_text("line1\nline2\n", encoding="utf-8")
    restarted: list[str] = []

    config = HealthConfig(
        health_url="http://127.0.0.1:1/api/v1/health",
        state_file=state,
        health_log=log,
        timeout_s=0.05,
        failure_threshold=2,
        restart_command=(sys.executable, "-c", "print('restarted')"),
        engine_log_path=engine_log,
    )
    run_health_tick(
        config,
        probe=lambda _url, _timeout: "timeout",
        restart=lambda _cmd: restarted.append("yes") or (True, "kickstart ok"),
        tail_log=lambda path, lines: path.read_text(encoding="utf-8").splitlines()[-lines:],
    )
    code = run_health_tick(
        config,
        probe=lambda _url, _timeout: "timeout",
        restart=lambda _cmd: restarted.append("yes") or (True, "kickstart ok"),
        tail_log=lambda path, lines: path.read_text(encoding="utf-8").splitlines()[-lines:],
    )

    assert code == 2
    assert restarted == ["yes"]
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["event"] == "restart"
    assert rows[-1]["engine_log_tail"] == ["line1", "line2"]


def test_healthy_probe_resets_failures(tmp_path: Path) -> None:
    """If health returns 200 then consecutive failures reset to zero."""
    log = tmp_path / "health.jsonl"
    state = tmp_path / "state.json"
    state.write_text('{"consecutive_failures": 2}', encoding="utf-8")

    code = run_health_tick(
        HealthConfig(
            health_url="http://127.0.0.1:8728/api/v1/health",
            state_file=state,
            health_log=log,
            timeout_s=1.0,
            failure_threshold=2,
            restart_command=("true",),
            engine_log_path=tmp_path / "engine.log",
        ),
        probe=lambda _url, _timeout: None,
        restart=lambda _cmd: (True, "unused"),
        tail_log=lambda _path, _lines: [],
    )

    assert code == 0
    assert json.loads(state.read_text(encoding="utf-8"))["consecutive_failures"] == 0
    assert '"event": "healthy"' in log.read_text(encoding="utf-8")
