"""Shared `gh api -X` JSON-body sender (`scripts/gh_api_send.py`).

[if] gh is missing, the send exits non-zero, or it succeeds [then] the helper
raises the caller-supplied error or returns stdout with the original argv, [else stop].
"""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts.gh_api_send import gh_send


class SendError(Exception):
    """Stand-in so wrapping is independent of LedgerError / MapError."""


def test_missing_gh_raises_the_passed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] gh is not on PATH [then] gh_send raises the caller-supplied error, [else stop]."""
    monkeypatch.setattr("scripts.gh_api_send.shutil.which", lambda name: None)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subprocess.run must not run when gh is missing")

    monkeypatch.setattr("scripts.gh_api_send.subprocess.run", boom)
    with pytest.raises(SendError, match="gh is not on PATH"):
        gh_send("POST", "repos/o/r/git/blobs", {"content": "x"}, error=SendError)


def test_nonzero_exit_raises_the_passed_error_with_stripped_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] gh api -X exits non-zero [then] gh_send raises the caller-supplied error with stripped stderr, [else stop]."""
    monkeypatch.setattr("scripts.gh_api_send.shutil.which", lambda name: "gh-resolved")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout=b"", stderr=b"  boom \xff \n")

    monkeypatch.setattr("scripts.gh_api_send.subprocess.run", fake_run)
    with pytest.raises(SendError) as caught:
        gh_send("PUT", "repos/o/r/git/refs/heads/ci-data", {"sha": "abc"}, error=SendError)
    assert str(caught.value) == "gh api -X PUT repos/o/r/git/refs/heads/ci-data exited 1: boom \ufffd"


def test_success_returns_stdout_and_passes_argv_input_and_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] gh api -X succeeds [then] gh_send returns stdout and the argv, stdin JSON, timeout, and check=False match the copies, [else stop]."""
    captured: dict[str, object] = {}
    body = {"content": "ledger", "encoding": "utf-8"}
    stdout = b'{"sha":"abc"}'
    monkeypatch.setattr("scripts.gh_api_send.shutil.which", lambda name: "gh-resolved")

    def fake_run(
        argv: list[str],
        *,
        input: bytes,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["input"] = input
        captured["capture_output"] = capture_output
        captured["timeout"] = timeout
        captured["check"] = check
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr("scripts.gh_api_send.subprocess.run", fake_run)
    got = gh_send("POST", "repos/o/r/git/blobs", body, timeout=45, error=SendError)
    assert got == stdout
    assert captured["argv"] == [
        "gh-resolved",
        "api",
        "-X",
        "POST",
        "repos/o/r/git/blobs",
        "--input",
        "-",
    ]
    assert captured["input"] == json.dumps(body).encode()
    assert captured["capture_output"] is True
    assert captured["timeout"] == 45
    assert captured["check"] is False
