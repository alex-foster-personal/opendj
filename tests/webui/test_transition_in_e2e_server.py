"""Unit tests for the transition-in cue Playwright engine launcher (issue #7916)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support import transition_in_e2e_server

_LAUNCHER = Path(transition_in_e2e_server.__file__)
_CONFIG = Path(__file__).resolve().parents[2] / "apps/webui/frontend/tests/e2e/playwright.transition-in-cue.config.ts"
_FIXTURE_MODULE = "apps.webui.frontend.tests.e2e.support.transition_in_fixture"


def test_launcher_module_has_no_early_webui_imports() -> None:
    head = _LAUNCHER.read_text(encoding="utf-8").split("def main", 1)[0]
    assert not re.search(r"^\s*(?:from|import)\s+.*transition_in_fixture", head, re.M)
    assert not re.search(r"^\s*(?:from|import)\s+apps\.webui\.server", head, re.M)


def test_main_seeds_fixture_then_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runs: list[list[str]] = []
    serve_calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, check: bool) -> object:
        _ = check
        runs.append(cmd)
        return type("R", (), {"returncode": 0})()

    def fake_serve(argv: list[str]) -> int:
        serve_calls.append(argv)
        return 0

    monkeypatch.setattr(transition_in_e2e_server.subprocess, "run", fake_run)
    import apps.engine_core.__main__ as engine_main

    monkeypatch.setattr(engine_main, "main", fake_serve)

    data_dir = str(tmp_path)
    rc = transition_in_e2e_server.main(["--data-dir", data_dir, "--port", "8714"])
    assert rc == 0
    assert len(runs) == 1
    assert runs[0][:3] == [runs[0][0], "-m", _FIXTURE_MODULE]
    assert runs[0][runs[0].index("--data-dir") + 1] == data_dir
    assert serve_calls == [["serve", "--data-dir", data_dir, "--host", "127.0.0.1", "--port", "8714"]]


def test_fixture_seed_failure_skips_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    serve_called = False

    def fake_run(cmd: list[str], *, check: bool) -> object:
        _ = check, cmd
        return type("R", (), {"returncode": 2})()

    def fake_serve(argv: list[str]) -> int:
        nonlocal serve_called
        serve_called = True
        return 0

    monkeypatch.setattr(transition_in_e2e_server.subprocess, "run", fake_run)
    import apps.engine_core.__main__ as engine_main

    monkeypatch.setattr(engine_main, "main", fake_serve)

    rc = transition_in_e2e_server.main(["--data-dir", str(tmp_path), "--port", "8714"])
    assert rc == 2
    assert serve_called is False


def test_playwright_config_uses_python_launcher_not_shell_pipeline() -> None:
    text = _CONFIG.read_text(encoding="utf-8")
    assert "transition_in_e2e_server" in text
    engine_region = text.split("transition-in-cue-engine", 1)[1].split("transition-in-cue-vite", 1)[0]
    assert "&&" not in engine_region
    assert "mkdir" not in engine_region
