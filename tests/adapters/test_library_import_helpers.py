"""Unit tests for small ``library_import`` helpers."""

from __future__ import annotations

import pytest

from apps.adapters.library_import import _location_entries_dropped
from apps.open_dj.schema import AdapterReport


@pytest.mark.requirement("DJIMPORT-6039")
def test_location_entries_dropped_counts_only_location_dropped() -> None:
    """[if] mixed adapter warnings [then] only location dropped rows count, [else stop]."""
    report = AdapterReport()
    report.warn(field="location", track_id="", action="dropped", reason="no path")
    report.warn(field="location", track_id="", action="dropped", reason="no path again")
    report.warn(field="bpm", track_id="1", action="dropped", reason="bad bpm")
    report.warn(field="location", track_id="1", action="downgraded", reason="streaming")
    assert _location_entries_dropped(report) == 2
