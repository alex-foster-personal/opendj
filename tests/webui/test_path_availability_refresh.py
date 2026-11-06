"""Lifecycle of the process-wide path-availability-refresh singleton.

Overlapping start_for_state_db must join the replaced thread (issue #8179).
"""

from __future__ import annotations

import threading
from pathlib import Path

from apps.webui.server import path_availability_refresh as par


def _live(name: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]


def test_overlapping_start_for_state_db_joins_the_replaced_thread(tmp_path: Path) -> None:
    """[if] two start_for_state_db calls overlap [then] the replaced thread is joined, [else stop]."""
    par.stop()
    try:
        baseline = _live("path-availability-refresh")
        par.start_for_state_db(tmp_path / "eng-a" / "state" / "state.db")
        first = [t for t in _live("path-availability-refresh") if t not in baseline]
        assert len(first) == 1
        par.start_for_state_db(tmp_path / "eng-b" / "state" / "state.db")
        assert not first[0].is_alive()
        replacement = [t for t in _live("path-availability-refresh") if t not in baseline]
        assert len(replacement) == 1
        par.stop()
        assert "refresher" not in par._ACTIVE
        par.stop()
        leftover = [t for t in _live("path-availability-refresh") if t not in baseline]
        assert leftover == []
    finally:
        par.stop()
