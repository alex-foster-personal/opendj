"""Background-priority seam for child processes (portable by default).

Regression lines:
  - if the POSIX arm does not lower the child's niceness then background work competes with playback
  - if the Windows arm passes preexec_fn then subprocess refuses to start the child at all
  - if ahead analysis bypasses the seam then its Windows lane never runs
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from apps.shared import process_priority
from apps.webui.server import ahead_analysis as aa


@pytest.mark.skipif(sys.platform == "win32", reason="os.nice is POSIX-only; Windows arm below")
def test_posix_child_runs_at_the_lowered_niceness() -> None:
    probe = [sys.executable, "-c", "import os; print(os.nice(0))"]
    plain = subprocess.run(probe, capture_output=True, text=True, check=True)
    lowered = subprocess.run(
        probe, capture_output=True, text=True, check=True, **process_priority.lowered_priority(5)
    )
    assert int(lowered.stdout) == min(int(plain.stdout) + 5, 19)


def test_windows_arm_uses_a_priority_class_not_preexec_fn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_priority.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    assert process_priority.lowered_priority(5) == {"creationflags": 0x4000}


def test_ahead_analysis_starts_its_queue_child_through_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(process_priority.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    monkeypatch.setattr(aa.subprocess, "run", fake_run)
    assert aa._queue_cli(["status"], os.devnull)[0] == 0
    assert "preexec_fn" not in seen and seen["creationflags"] == 0x4000


def test_windows_self_lowering_sets_a_priority_class_not_os_nice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    calls: list[tuple[str, int]] = []

    class Kernel32:
        def GetCurrentProcess(self) -> int:
            return -1

        def SetPriorityClass(self, handle: int, priority: int) -> int:
            calls.append(("SetPriorityClass", priority))
            return 1

    monkeypatch.setattr(process_priority.sys, "platform", "win32")
    def win_dll(name: str, *, use_last_error: bool = False) -> Kernel32:
        assert name == "kernel32" and use_last_error
        return Kernel32()

    monkeypatch.setattr(ctypes, "WinDLL", win_dll, raising=False)
    monkeypatch.delattr(process_priority.os, "nice", raising=False)
    process_priority.lower_own_priority(19)
    assert calls == [("SetPriorityClass", 0x4000)]
