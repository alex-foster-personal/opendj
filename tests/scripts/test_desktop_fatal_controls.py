"""DESKTOP-11280: fatal, relaunch, and stop-or-quit controls must pending and fail visibly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = REPO_ROOT / "apps/desktop/src-tauri/src/main.rs"
SUPERVISOR_RS = REPO_ROOT / "apps/desktop/src-tauri/src/supervisor.rs"
ELECTRON_MAIN = REPO_ROOT / "apps/desktop/electron/src/main.ts"
ELECTRON_SUPERVISOR = REPO_ROOT / "apps/desktop/electron/src/supervisor.ts"


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"fn {re.escape(name)}\b", source)
    assert match is not None, f"missing fn {name}"
    start = match.start()
    brace = source.find("{", match.end())
    assert brace != -1
    depth = 0
    for idx in range(brace, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"unclosed fn {name}")


@pytest.mark.requirement("DESKTOP-11280")
def test_fail_visibly_shows_ok_dialog_before_exit() -> None:
    """[if] fail_visibly omits the Ok dialog [then] startup errors go silent, [else stop]."""
    body = _function_body(MAIN_RS.read_text(encoding="utf-8"), "fail_visibly")
    assert "MessageButtons::Ok" in body
    assert ".show()" in body
    assert "std::process::exit(1)" in body


@pytest.mark.requirement("DESKTOP-11280")
def test_stop_or_quit_logs_while_stopping_the_holder() -> None:
    """[if] Stop engine is chosen with no in-flight log [then] the boot path is silent, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "user chose to stop engine pid" in main_rs


@pytest.mark.requirement("DESKTOP-11280")
def test_user_requested_restart_publishes_restarting_before_spawn() -> None:
    """[if] user relaunch leaves phase fatal while restart runs [then] no pending work shows, [else stop]."""
    supervisor_rs = SUPERVISOR_RS.read_text(encoding="utf-8")
    assert "user_requested" in supervisor_rs
    assert "SupervisorPhase::Restarting" in supervisor_rs
    assert "update_surfaces(app, supervisor, &guard)" in supervisor_rs


@pytest.mark.requirement("DESKTOP-11280")
def test_electron_restart_publishes_restarting_before_spawn() -> None:
    """[if] Electron relaunch leaves phase fatal while restart runs [then] no pending work shows, [else stop]."""
    text = ELECTRON_SUPERVISOR.read_text(encoding="utf-8")
    attempt_idx = text.index("private async attemptRestart")
    attempt = text[attempt_idx : attempt_idx + 400]
    assert "this.phase = 'restarting'" in attempt
    assert "this.updateSurfaces()" in attempt


@pytest.mark.requirement("DESKTOP-11280")
def test_electron_stop_or_quit_logs_while_stopping_the_holder() -> None:
    """[if] Electron Stop engine is chosen with no in-flight log [then] boot is silent, [else stop]."""
    assert "user chose to stop engine pid" in ELECTRON_MAIN.read_text(encoding="utf-8")
