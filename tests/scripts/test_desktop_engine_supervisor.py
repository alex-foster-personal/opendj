"""INSTALL-23 source scans for runtime engine supervision (issue #2916)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_SRC = REPO_ROOT / "apps/desktop/src-tauri/src"
MAIN_RS = TAURI_SRC / "main.rs"
LAUNCH_RS = TAURI_SRC / "launch.rs"
ENGINE_RS = TAURI_SRC / "engine.rs"
SUPERVISOR_RS = TAURI_SRC / "supervisor.rs"
SHELL_HEALTH_RS = TAURI_SRC / "shell_health.rs"
SETUP_JS = REPO_ROOT / "apps/desktop/setup/setup.js"
SETUP_HTML = REPO_ROOT / "apps/desktop/setup/index.html"


@pytest.mark.requirement("INSTALL-23")
def test_supervisor_module_exists_with_runtime_poll() -> None:
    """[if] no supervisor thread polls the child [then] zombies linger, [else stop]."""
    assert SUPERVISOR_RS.is_file(), "supervisor.rs must exist"
    source = SUPERVISOR_RS.read_text(encoding="utf-8")
    assert "try_wait" in source
    assert "thread::spawn" in source or "std::thread::spawn" in source


@pytest.mark.requirement("INSTALL-23")
def test_pid_can_act_detects_zombies() -> None:
    """[if] pid_alive alone gates liveness [then] zombies look alive, [else stop]."""
    launch_rs = LAUNCH_RS.read_text(encoding="utf-8")
    assert "pid_can_act" in launch_rs
    assert re.search(r"['\"]Z['\"]", launch_rs) or "Z" in launch_rs
    assert "inspect_lock" in launch_rs
    assert "pid_can_act" in launch_rs.split("inspect_lock")[0] or "pid_can_act(pid)" in launch_rs


@pytest.mark.requirement("INSTALL-23")
def test_shell_health_server_reports_engine_dead() -> None:
    """[if] shell health omits engine: dead [then] agents spin on stale port, [else stop]."""
    assert SHELL_HEALTH_RS.is_file(), "shell_health.rs must exist"
    source = SHELL_HEALTH_RS.read_text(encoding="utf-8")
    assert "/api/v1/health" in source
    assert "engine.shell.json" in source or ".engine.shell.json" in source
    assert '"dead"' in source or "'dead'" in source


@pytest.mark.requirement("INSTALL-23")
def test_title_bar_shows_engine_dead() -> None:
    """[if] title stays product name when engine dies [then] user sees no signal, [else stop]."""
    supervisor = SUPERVISOR_RS.read_text(encoding="utf-8")
    assert "engine dead" in supervisor
    assert "set_title" in supervisor


@pytest.mark.requirement("INSTALL-23")
def test_restart_logs_exit_code_with_utc_timestamp() -> None:
    """[if] restart omits the audit line [then] silver incident is unprovable, [else stop]."""
    supervisor = SUPERVISOR_RS.read_text(encoding="utf-8")
    # The line is "<utc> engine restarted after {reason}", where the reason is
    # "exit code N" for an exit (or names the health-check silence instead).
    assert "engine restarted after {reason}" in supervisor
    assert 'format!("exit code {}"' in supervisor
    assert "+00:00" in supervisor or "format_utc" in supervisor or "utc_timestamp" in supervisor


@pytest.mark.requirement("INSTALL-23")
def test_supervisor_skipped_when_engine_origin_env_set() -> None:
    """[if] supervisor runs with OPENDJ_ENGINE_ORIGIN [then] dev attach gets restarts, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    supervisor = SUPERVISOR_RS.read_text(encoding="utf-8")
    assert "OPENDJ_ENGINE_ORIGIN" in main_rs
    assert "start_runtime_supervisor" in supervisor or "RuntimeSupervisor" in supervisor
    origin_idx = main_rs.index("OPENDJ_ENGINE_ORIGIN")
    supervisor_start = main_rs.index("start_runtime_supervisor")
    assert origin_idx < supervisor_start


@pytest.mark.requirement("INSTALL-23")
def test_fatal_bootstrap_view_exists() -> None:
    """[if] mid-session death leaves the SPA spinning [then] UX is broken, [else stop]."""
    setup_js = SETUP_JS.read_text(encoding="utf-8")
    setup_html = SETUP_HTML.read_text(encoding="utf-8")
    assert "fatal-view" in setup_html or "fatal_view" in setup_js
    assert "Relaunch" in setup_html or "relaunch" in setup_js


@pytest.mark.requirement("INSTALL-23")
def test_failed_restart_does_not_publish_restarting() -> None:
    """[if] failed attempt_restart falls through to publish_restarting [then] health lies, [else stop]."""
    source = SUPERVISOR_RS.read_text(encoding="utf-8")
    assert re.search(
        r"else if guard\.phase == SupervisorPhase::Restarting\s*\{[^}]*publish_restarting",
        source,
    )
    assert "} else {\n                publish_restarting" not in source


@pytest.mark.requirement("INSTALL-23")
def test_main_wires_supervisor_modules() -> None:
    """[if] main.rs does not import supervisor [then] nothing runs, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "mod supervisor" in main_rs
    assert "mod shell_health" in main_rs
    assert "start_runtime_supervisor" in main_rs


@pytest.mark.requirement("LOGS-05")
def test_engine_log_disk_guard_constants_in_rust_modules() -> None:
    """[if] low-disk constants are absent [then] rotation cannot halt, [else stop]."""
    engine_log = (TAURI_SRC / "engine_log.rs").read_text(encoding="utf-8")
    supervisor = SUPERVISOR_RS.read_text(encoding="utf-8")
    assert "ENGINE_LOG_MIN_FREE_BYTES" in engine_log
    assert "disk_free_bytes" in engine_log
    assert "ENGINE_LOG_MAX_ARCHIVE_COUNT" in engine_log
    assert "AwaitingDiskSpace" in supervisor
    assert "engine restarted after low disk recovered" in supervisor
