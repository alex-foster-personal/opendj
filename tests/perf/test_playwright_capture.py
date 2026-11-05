"""Contract tests for the shared Playwright KPI capture runner."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.perf import playwright_capture


def _run_capture(
    tmp_path: Path,
    *,
    google_storage_state: str | None = None,
) -> dict[str, Any]:
    return playwright_capture.run_playwright_capture(
        engine="http://engine.test/",
        frontend="http://frontend.test/",
        frontend_root=tmp_path,
        playwright_config="tests/e2e/playwright.test.config.ts",
        timeout_s=37,
        google_storage_state=google_storage_state,
    )


def test_success_preserves_command_environment_result_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, **kwargs)
        result_path = Path(kwargs["env"]["KPI_CAPTURE_RESULT"])
        captured["result_path"] = result_path
        result_path.write_text(json.dumps({"ok": True, "span": {"duration_ms": 12}}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="capture output", stderr="")

    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: "/tools/pnpm")
    monkeypatch.setattr(playwright_capture.subprocess, "run", _fake_run)

    result = _run_capture(tmp_path)

    assert result == {"ok": True, "span": {"duration_ms": 12}}
    assert captured["command"] == [
        "/tools/pnpm",
        "exec",
        "playwright",
        "test",
        "--config",
        "tests/e2e/playwright.test.config.ts",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False
    env = captured["env"]
    assert env["PERFORMANCE_E2E_START_SERVERS"] == "0"
    assert env["PERFORMANCE_E2E_BASE_URL"] == "http://frontend.test"
    assert env["PERFORMANCE_E2E_API_BASE"] == "http://engine.test"
    assert env["KPI_CAPTURE_ENGINE"] == "http://engine.test"
    assert env["KPI_CAPTURE_FRONTEND"] == "http://frontend.test"
    assert env["KPI_CAPTURE_TIMEOUT_S"] == "37"
    assert "KPI_CAPTURE_GOOGLE_STORAGE_STATE" not in env
    assert not captured["result_path"].exists()


@pytest.mark.parametrize(
    ("storage_state", "expected"),
    [(None, None), ("", None), ("state.json", "state.json")],
)
def test_google_storage_state_is_set_only_when_nonempty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_state: str | None,
    expected: str | None,
) -> None:
    captured_env: dict[str, str] = {}
    monkeypatch.delenv("KPI_CAPTURE_GOOGLE_STORAGE_STATE", raising=False)

    def _fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_env.update(kwargs["env"])
        Path(kwargs["env"]["KPI_CAPTURE_RESULT"]).write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: "/tools/pnpm")
    monkeypatch.setattr(playwright_capture.subprocess, "run", _fake_run)

    _run_capture(tmp_path, google_storage_state=storage_state)

    assert captured_env.get("KPI_CAPTURE_GOOGLE_STORAGE_STATE") == expected


@pytest.mark.parametrize(
    "write_result",
    [
        lambda path: path.write_text("not-json", encoding="utf-8"),
        lambda path: path.unlink(),
    ],
)
def test_invalid_or_unreadable_result_returns_missing_result_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_result: Callable[[Path], object],
) -> None:
    result_path: Path | None = None

    def _fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal result_path
        result_path = Path(kwargs["env"]["KPI_CAPTURE_RESULT"])
        write_result(result_path)
        return subprocess.CompletedProcess(command, 6, stdout="stdout detail", stderr="stderr detail")

    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: "/tools/pnpm")
    monkeypatch.setattr(playwright_capture.subprocess, "run", _fake_run)

    result = _run_capture(tmp_path)

    assert result == {
        "ok": False,
        "span": None,
        "reason": "playwright capture did not write KPI_CAPTURE_RESULT (exit 6)",
    }
    assert result_path is not None
    assert not result_path.exists()


def test_blank_success_result_preserves_existing_empty_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: "/tools/pnpm")
    monkeypatch.setattr(playwright_capture.subprocess, "run", _fake_run)

    assert _run_capture(tmp_path) == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"detail": "kept"},
            {
                "detail": "kept",
                "ok": False,
                "span": None,
                "reason": "playwright exited 4: stderr detail",
            },
        ),
        ({"reason": "captured reason"}, {"reason": "captured reason"}),
        (
            {"ok": True, "span": {"duration_ms": 1}},
            {
                "ok": True,
                "span": {"duration_ms": 1},
                "reason": "playwright exited 4: stderr detail",
            },
        ),
    ],
)
def test_nonzero_exit_fills_only_missing_fields_and_preserves_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    def _fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(kwargs["env"]["KPI_CAPTURE_RESULT"]).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 4, stdout="stdout detail", stderr="stderr detail")

    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: "/tools/pnpm")
    monkeypatch.setattr(playwright_capture.subprocess, "run", _fake_run)

    assert _run_capture(tmp_path) == expected


def test_nonzero_exit_uses_stdout_when_stderr_is_blank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(kwargs["env"]["KPI_CAPTURE_RESULT"]).write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 2, stdout="stdout detail", stderr="  ")

    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: "/tools/pnpm")
    monkeypatch.setattr(playwright_capture.subprocess, "run", _fake_run)

    assert _run_capture(tmp_path)["reason"] == "playwright exited 2: stdout detail"


def test_missing_pnpm_fails_before_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playwright_capture.shutil, "which", lambda _executable: None)

    with pytest.raises(RuntimeError, match="pnpm executable was not found"):
        _run_capture(tmp_path)
