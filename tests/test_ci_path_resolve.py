"""The shared CI-gate PATH resolve helper names missing programs with the caller's purpose phrase."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts.ci_path_resolve import require_on_path, resolve_on_path


def test_resolve_on_path_pick_race_purpose_phrase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(
        SystemExit,
        match=r"^\[ERROR\] pnpm is not on PATH; the pick-race gate cannot run without it$",
    ):
        resolve_on_path("pnpm", without="the pick-race gate")


def test_resolve_on_path_cue_preview_purpose_phrase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(
        SystemExit,
        match=r"^\[ERROR\] gcc is not on PATH; the cue-preview gate cannot run without it$",
    ):
        resolve_on_path("gcc", without="the cue-preview gate")


def test_resolve_on_path_returns_which_result(monkeypatch: pytest.MonkeyPatch) -> None:
    interpreter = Path(sys.executable)
    monkeypatch.setenv("PATH", str(interpreter.parent))
    found = resolve_on_path(interpreter.name, without="the pick-race gate")
    assert os.path.samefile(found, interpreter)


def test_require_on_path_raises_caller_exception_with_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] git is absent from PATH [then] the caller's exception and message are raised, [else stop]."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match=r"^git is not on PATH: probe$"):
        require_on_path("git", RuntimeError, "git is not on PATH: probe")


class _ProbeExc(Exception):
    """Test-only exception so require_on_path is not tied to one type."""


def test_require_on_path_accepts_non_system_exit_type(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the tool is missing [then] exc_type may differ from SystemExit, [else stop]."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(_ProbeExc, match=r"^missing$"):
        require_on_path("git", _ProbeExc, "missing")


def test_require_on_path_returns_which_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] PATH contains only the interpreter dir [then] require_on_path returns that binary, [else stop]."""
    interpreter = Path(sys.executable)
    monkeypatch.setenv("PATH", str(interpreter.parent))
    found = require_on_path(interpreter.name, RuntimeError, "probe")
    assert os.path.samefile(found, interpreter)
