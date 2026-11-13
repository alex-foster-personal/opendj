"""A stalled error sink can finish a pass again: its mark is capped at MAX_CATCH_UP_HOURS."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.ci_run_batch import catch_up_since

NOW = datetime(2026, 11, 12, 21, 33, tzinfo=UTC)
DAY = timedelta(hours=24)
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci-error-sink.yml"


def test_a_five_week_old_mark_is_raised_and_names_the_unread_window() -> None:
    since, unread = catch_up_since("2026-10-07T16:45:19Z", NOW, DAY)
    assert since == "2026-11-11T21:33:00Z"
    assert unread == "2026-10-07T16:45:19Z..2026-11-11T21:33:00Z"


def test_a_recent_mark_is_kept_and_nothing_is_unread() -> None:
    assert catch_up_since("2026-11-12T19:33:00Z", NOW, DAY) == ("2026-11-12T19:33:00Z", None)


def test_a_mark_exactly_at_the_cap_is_kept() -> None:
    assert catch_up_since("2026-11-11T21:33:00Z", NOW, DAY) == ("2026-11-11T21:33:00Z", None)


def test_the_sink_workflow_passes_its_cap_to_the_pass() -> None:
    text = WORKFLOW.read_text()
    assert re.search(r'^\s+MAX_CATCH_UP_HOURS: "\d+"$', text, re.M)
    assert '--max-catch-up-hours "$MAX_CATCH_UP_HOURS"' in text
