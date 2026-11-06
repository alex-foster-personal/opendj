"""Local stems worker drops scheduling priority through the portable seam.

[if] run() starts [then] it calls the priority seam and reports honestly, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import stems_local_worker as worker

pytestmark = pytest.mark.requirement("PERFBATCH-04")


class _FakeProtocol(worker._Protocol):
    def __init__(self) -> None:
        self.messages: list[tuple[float, str, dict[str, Any]]] = []

    def emit(self, progress: float, message: str, **extra: Any) -> None:
        self.messages.append((progress, message, extra))

    def close(self) -> None:
        return None


def test_run_lowers_priority_through_portable_seam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] lowering succeeds [then] progress reports requested niceness, [else stop]."""
    requested: list[int] = []
    protocol = _FakeProtocol()

    monkeypatch.setattr(worker, "lower_own_priority", requested.append)

    assert worker.run([], tmp_path, protocol) == 0
    assert requested == [worker.NICE_LEVEL]
    assert protocol.messages[0][1] == (f"0/0 local stems (htdemucs, nice={worker.NICE_LEVEL})")


def test_run_reports_priority_failure_honestly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] lowering fails [then] progress reports UNAVAILABLE, [else stop]."""
    protocol = _FakeProtocol()

    def fail_to_lower(_niceness: int) -> None:
        raise OSError("SetPriorityClass failed")

    monkeypatch.setattr(worker, "lower_own_priority", fail_to_lower)

    assert worker.run([], tmp_path, protocol) == 0
    messages = "\n".join(message for _progress, message, _extra in protocol.messages)
    assert "nice=UNAVAILABLE" in messages
    assert f"nice={worker.NICE_LEVEL}" not in messages
