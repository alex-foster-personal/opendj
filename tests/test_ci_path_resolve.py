"""The shared CI-gate PATH resolve helper names missing programs with the caller's purpose phrase."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts.ci_path_resolve import resolve_on_path


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
