"""Regression tests for the self-contained installed-app vocal worker.

Acceptance tests:

- if the packaged launcher supplies its runtime [then] vocals uses that
  runtime and worker script without consulting the host toolchain
- if the runtime or worker script cannot be started [then] the operator gets
  a plain-language recovery message with no implementation detail
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.vocals import cli as vocals_cli


class _FakeWorkerProcess:
    returncode = 0
    pid = 12345

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return '{"ok": true}', ""


def test_packaged_worker_uses_payload_runtime_and_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MDT_VOCAL_WORKER_PYTHON", "/payload/runtime/bin/python3")
    monkeypatch.setenv("MDT_VOCAL_WORKER_SCRIPT", "/payload/scripts/vocal_region_worker.py")
    command = vocals_cli._worker_command(Path("/music/track.wav"))
    assert command == [
        "/payload/runtime/bin/python3",
        "/payload/scripts/vocal_region_worker.py",
        "--device",
        "auto",
        "/music/track.wav",
    ]


def test_missing_worker_runtime_has_plain_language_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_popen(_command: list[str], **_kwargs: Any) -> _FakeWorkerProcess:
        raise FileNotFoundError("missing executable")

    monkeypatch.setenv("MDT_VOCAL_WORKER_PYTHON", "/missing/python3")
    monkeypatch.setattr(vocals_cli.subprocess, "Popen", missing_popen)

    with pytest.raises(RuntimeError) as excinfo:
        vocals_cli.run_worker(Path("/music/track.wav"))

    message = str(excinfo.value)
    assert "Vocal separation is unavailable" in message
    assert "reinstall open dj from a complete dmg" in message.lower()
    assert "FileNotFoundError" not in message
    assert "uv" not in message.lower()


def test_payload_launcher_exports_vocal_runtime_contract() -> None:
    from scripts.build_engine_payload import LAUNCHER_TEMPLATE

    assert "MDT_VOCAL_WORKER_PYTHON=\"$payload/runtime/bin/python3\"" in LAUNCHER_TEMPLATE
    assert (
        "MDT_VOCAL_WORKER_SCRIPT=\"$payload/app/scripts/vocal_region_worker.py\""
        in LAUNCHER_TEMPLATE
    )
