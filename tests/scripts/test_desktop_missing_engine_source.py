"""LAUNCH-10680: missing engine source directory refuses spawn (issue #10306)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_SRC = REPO_ROOT / "apps/desktop/src-tauri/src"
MAIN_RS = TAURI_SRC / "main.rs"
ENGINE_RS = TAURI_SRC / "engine.rs"
OVERRIDE_RS = TAURI_SRC / "engine_dev_override.rs"
SHELL_HEALTH_RS = TAURI_SRC / "shell_health.rs"
SETUP_JS = REPO_ROOT / "apps/desktop/setup/setup.js"
SETUP_HTML = REPO_ROOT / "apps/desktop/setup/index.html"


@pytest.mark.requirement("LAUNCH-10680")
def test_boot_resolves_override_before_engine_spawn() -> None:
    """[if] spawn runs before override resolve [then] crash-loop continues, [else stop]."""
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "resolve_payload_for_spawn" in main_rs
    assert "MissingEngineSource" in main_rs
    assert "missing-source" in main_rs
    assert "start_engine(&handle" in main_rs
    resolve_idx = main_rs.index("resolve_payload_for_spawn")
    spawn_idx = main_rs.index("start_engine(&handle")
    assert resolve_idx < spawn_idx


@pytest.mark.requirement("LAUNCH-10680")
def test_bootstrap_fatal_page_names_path_key_and_reset() -> None:
    """[if] fatal page hides path or reset [then] preview loop is opaque, [else stop]."""
    html = SETUP_HTML.read_text(encoding="utf-8")
    js = SETUP_JS.read_text(encoding="utf-8")
    assert "missing-source-view" in html
    assert "missing-source-path" in html
    assert "missing-source-key" in html
    assert "Reset to bundled payload" in html
    assert "parseMissingEngineSourceQuery" in js
    assert "resetBundledPayload" in js


@pytest.mark.requirement("LAUNCH-10680")
def test_reset_clears_override_via_shell_health() -> None:
    """[if] reset cannot clear override [then] user is stuck on bundled path, [else stop]."""
    health = SHELL_HEALTH_RS.read_text(encoding="utf-8")
    override = OVERRIDE_RS.read_text(encoding="utf-8")
    assert "/api/v1/reset-bundled-payload" in health
    assert "clear_configured_engine_source" in health
    assert "take_reset_bundled_payload_request" in health
    assert "request_restart" in MAIN_RS.read_text(encoding="utf-8")
    assert "engine-dev-override.json" in override or "engine_dev_override" in override


@pytest.mark.requirement("LAUNCH-10680")
def test_resolver_keeps_bundled_path_without_override() -> None:
    """[if] no override changes resolve [then] regressions ship silently, [else stop]."""
    engine_rs = ENGINE_RS.read_text(encoding="utf-8")
    assert "resolve_engine_payload_dir" in engine_rs
    assert "MissingEngineSource" in engine_rs
    assert "payload_tree_for_existing_source" in engine_rs
