"""A schedule_plan while another process holds the schedule lock is refused busy, not loaded over (AGENT-29)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.opendj_cli import dj_service
from apps.opendj_cli.dj_service import SCHEDULE_LOCK_NAME, DjError, DjService
from apps.opendj_cli.origin import EngineOrigin


def _service(tmp_path: Path) -> DjService:
    origin = EngineOrigin(
        host="127.0.0.1", port=1, lock_path=tmp_path / ".engine.lock", pid=None, role=None, boot_id=None
    )
    return DjService(origin=origin)


@pytest.mark.requirement("AGENT-29")
def test_a_schedule_while_another_process_holds_the_lock_is_refused_plan_busy(tmp_path: Path) -> None:
    """[if] another holder has the schedule lock [then] schedule answers plan_busy and leaves the lock alone, [else stop]."""
    service = _service(tmp_path)
    # Control: with no lock held, the call gets past the lock and fails on the engine it cannot reach.
    with pytest.raises(Exception) as free:
        service.schedule({})
    assert not (isinstance(free.value, DjError) and free.value.code == "plan_busy")
    # Another holder: the OS lock is taken before the schedule runs.
    held = tmp_path / SCHEDULE_LOCK_NAME
    with dj_service._schedule_lock(held), pytest.raises(DjError) as busy:
        service.schedule({})
    assert busy.value.code == "plan_busy"
    assert held.exists(), "a refused schedule must not remove another holder's lock file"


@pytest.mark.requirement("AGENT-29")
def test_a_lock_file_left_by_a_crashed_process_does_not_block_a_schedule(tmp_path: Path) -> None:
    """[if] a crashed holder left the lock file behind, unheld [then] a schedule proceeds and the file stays, [else stop]."""
    held = tmp_path / SCHEDULE_LOCK_NAME
    held.write_text("4242 crashed\n", encoding="utf-8")
    with pytest.raises(Exception) as taken:
        _service(tmp_path).schedule({})
    assert not (isinstance(taken.value, DjError) and taken.value.code == "plan_busy")
    assert held.exists(), "the lock file is never removed, so no release can delete another holder's lock"


@pytest.mark.requirement("AGENT-29")
def test_the_lock_is_released_when_the_holder_exits(tmp_path: Path) -> None:
    """[if] the holder's transaction ends [then] the next schedule can take the lock, [else stop]."""
    held = tmp_path / SCHEDULE_LOCK_NAME
    with dj_service._schedule_lock(held):
        pass
    with dj_service._schedule_lock(held):
        pass
