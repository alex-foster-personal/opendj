"""INSTALL-14 source scans for engine lifecycle (issue #2160)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_CONF = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"
MAIN_RS = TAURI_CONF.parent / "src/main.rs"
ENGINE_RS = TAURI_CONF.parent / "src/engine.rs"
LAUNCH_RS = TAURI_CONF.parent / "src/launch.rs"


@pytest.mark.requirement("INSTALL-14")
def test_spawn_sets_parent_pid_env() -> None:
    """[if] spawn skips OPENDJ_PARENT_PID [then] parent death goes undetected, [else stop]."""
    engine_rs = ENGINE_RS.read_text(encoding="utf-8")
    assert '"OPENDJ_PARENT_PID"' in engine_rs
    assert ".env(" in engine_rs


@pytest.mark.requirement("INSTALL-14")
def test_the_shell_inspects_the_lock_before_spawn() -> None:
    """[if] main.rs spawns before inspecting the lock [then] a stale lock is missed, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    launch_rs = LAUNCH_RS.read_text(encoding="utf-8")
    assert "mod launch" in main_rs
    assert "inspect_lock" in launch_rs or "inspect_lock" in main_rs
    spawn_idx = main_rs.index("engine::spawn")
    inspect_idx = min(
        main_rs.index("launch::") if "launch::" in main_rs else len(main_rs),
        main_rs.index("inspect_lock") if "inspect_lock" in main_rs else len(main_rs),
    )
    assert inspect_idx < spawn_idx


@pytest.mark.requirement("INSTALL-14")
def test_exit_requested_shuts_down_the_engine() -> None:
    """[if] main.rs skips shutdown on final Exit [then] the child process leaks, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "RunEvent::ExitRequested" in main_rs
    assert "RunEvent::Exit" in main_rs
    assert "supervisor.shutdown()" in main_rs


@pytest.mark.requirement("INSTALL-21")
def test_exit_requested_prevents_exit_without_shutdown() -> None:
    """[if] ExitRequested calls shutdown [then] Cmd-Q kills audio before confirm, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "prevent_exit" in main_rs
    exit_requested_idx = main_rs.index("RunEvent::ExitRequested { api, .. }")
    exit_idx = main_rs.index("RunEvent::Exit =>")
    shutdown_idx = main_rs.rindex("supervisor.shutdown()")
    assert exit_requested_idx < exit_idx
    assert shutdown_idx > exit_idx
    before_exit = main_rs[exit_requested_idx:exit_idx]
    assert "supervisor.shutdown()" not in before_exit


@pytest.mark.requirement("INSTALL-21")
def test_close_requested_delegates_to_webview_hook() -> None:
    """[if] red-window close bypasses the quit hook [then] quit is immediate, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "CloseRequested" in main_rs
    assert "__OPENDJ_requestQuit" in main_rs


@pytest.mark.requirement("INSTALL-14")
def test_adopt_path_still_builds_the_window() -> None:
    """[if] adopting an engine skips window creation [then] no window opens, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "Adopted" in main_rs or "Adopt" in main_rs
    assert "WebviewWindowBuilder::new" in main_rs


@pytest.mark.requirement("INSTALL-14")
def test_fail_visibly_still_precedes_window_creation() -> None:
    """[if] fail_visibly runs after window creation [then] startup errors go silent, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert main_rs.index("fail_visibly") < main_rs.index("WebviewWindowBuilder::new")


@pytest.mark.requirement("INSTALL-14")
def test_stop_or_quit_dialog_names_the_holder_pid() -> None:
    """[if] the stop-or-quit dialog omits the holder pid [then] no one can tell, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    launch_rs = LAUNCH_RS.read_text(encoding="utf-8")
    combined = main_rs + launch_rs
    assert re.search(r"pid.*\{.*pid", combined) or "pid {pid}" in combined or "{pid}" in combined
