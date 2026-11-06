"""Unit tests for the shared ``gh api`` GET helper (issue #8201)."""

from __future__ import annotations

import re
import subprocess
from typing import Any

import pytest

from scripts.gh_api import gh_api

GH = "/usr/bin/gh"
PATH = "repos/example/music-dj-tools/commits/abc"


class DomainError(Exception):
    """Caller-supplied domain error, standing in for LedgerError / MapError."""


def test_missing_gh_raises_without_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] gh is not on PATH [then] raise error_cls and do not call subprocess.run, [else stop]."""

    monkeypatch.setattr("scripts.gh_api.shutil.which", lambda _name: None)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run must not run when gh is missing")

    monkeypatch.setattr("scripts.gh_api.subprocess.run", _boom)
    with pytest.raises(DomainError, match="gh is not on PATH"):
        gh_api(PATH, error_cls=DomainError)


def test_nonzero_exit_raises_with_decoded_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] gh api exits non-zero [then] raise error_cls with replace-decoded stderr, [else stop]."""

    captured: dict[str, Any] = {}

    monkeypatch.setattr("scripts.gh_api.shutil.which", lambda name: GH if name == "gh" else None)

    def _run(
        argv: list[str], *, capture_output: bool, timeout: float, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["capture_output"] = capture_output
        captured["timeout"] = timeout
        captured["check"] = check
        return subprocess.CompletedProcess(argv, 7, stdout=b"", stderr=b"denied \xff here")

    monkeypatch.setattr("scripts.gh_api.subprocess.run", _run)
    expected = f"gh api {PATH} exited 7: denied \ufffd here"
    with pytest.raises(DomainError, match=re.escape(expected)):
        gh_api(PATH, timeout=15, error_cls=DomainError)
    assert captured["argv"] == [GH, "api", PATH]
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert captured["timeout"] == 15


def test_success_returns_stdout_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] gh api exits 0 [then] return stdout bytes unchanged, [else stop]."""

    body = b'{"ok": true}\xff'
    monkeypatch.setattr("scripts.gh_api.shutil.which", lambda name: GH if name == "gh" else None)

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=body, stderr=b"")

    monkeypatch.setattr("scripts.gh_api.subprocess.run", _run)
    assert gh_api(PATH, error_cls=DomainError) == body


def test_timeout_expired_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] subprocess.run raises TimeoutExpired [then] it propagates unwrapped, [else stop]."""

    monkeypatch.setattr("scripts.gh_api.shutil.which", lambda name: GH if name == "gh" else None)

    def _run(argv: list[str], *, timeout: float, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr("scripts.gh_api.subprocess.run", _run)
    with pytest.raises(subprocess.TimeoutExpired):
        gh_api(PATH, timeout=9, error_cls=DomainError)
