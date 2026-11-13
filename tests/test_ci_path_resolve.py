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


_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bits; Windows resolves via PATHEXT")


def _fake_tool(directory: Path, name: str = "probe-tool") -> Path:
    """Write an executable stub named ``name`` into ``directory`` and return it."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return tool


@_POSIX_ONLY
def test_require_on_path_absolute_entry_returns_absolute_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] PATH holds an absolute dir with the tool [then] that absolute path is returned, [else stop]."""
    tool = _fake_tool(tmp_path / "abs")
    monkeypatch.setenv("PATH", str(tool.parent))
    assert require_on_path(tool.name, RuntimeError, "probe") == str(tool)


@_POSIX_ONLY
def test_require_on_path_relative_entry_resolves_inside_that_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] PATH holds a relative dir with the tool [then] the result names that dir, not a bare name, [else stop]."""
    _fake_tool(tmp_path / "bin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin")
    assert require_on_path("probe-tool", RuntimeError, "probe") == os.path.join("bin", "probe-tool")


@_POSIX_ONLY
def test_require_on_path_empty_entry_skips_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] an empty PATH entry precedes an absolute one [then] the absolute tool wins over the cwd copy, [else stop]."""
    _fake_tool(tmp_path)
    tool = _fake_tool(tmp_path / "abs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep + str(tool.parent))
    assert require_on_path(tool.name, RuntimeError, "probe") == str(tool)


@_POSIX_ONLY
def test_require_on_path_only_empty_entries_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] PATH has only empty entries and the cwd holds the tool [then] the caller's error is raised, [else stop]."""
    _fake_tool(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep)
    with pytest.raises(RuntimeError, match=r"^probe-tool is not on PATH$"):
        require_on_path("probe-tool", RuntimeError, "probe-tool is not on PATH")
