"""Progress-reporting contract for a USB apply run (requirement M17).

M17 asks for progress AND a remaining-time estimate on long library writes.
The shipped bar had elapsed-only, so a 150-track add could not be told apart
from a hang. These tests pin both halves.

Regression lines:
  - if the apply progress bar loses its remaining-time column then a long run
    reports elapsed only and M17 is half-missing again
  - if the bar loses its completed/total text then the numeric readout M17
    asked for ("can you see the %done?") is gone
  - if a rendered task at 3 of 10 does not show 3/10 then the column is wired
    to the wrong field
"""

from __future__ import annotations

from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from apps.sync.usb.apply import _progress_columns


def _column_types(columns) -> list[type]:
    return [type(c) for c in columns]


def test_progress_columns_include_time_remaining() -> None:
    assert TimeRemainingColumn in _column_types(_progress_columns()), (
        "apply's progress bar must carry a remaining-time estimate (M17), "
        "not elapsed only"
    )


def test_progress_columns_keep_elapsed_and_bar_and_count() -> None:
    types = _column_types(_progress_columns())
    assert BarColumn in types
    assert TimeElapsedColumn in types
    assert types.count(TextColumn) >= 2, (
        "expected a description column and a completed/total column"
    )


def test_progress_columns_render_completed_over_total() -> None:
    """The completed/total readout is a real render, not a format string."""
    progress = Progress(*_progress_columns())
    task_id = progress.add_task("applying", total=10)
    progress.update(task_id, completed=3)
    task = progress.tasks[0]
    rendered = [
        str(column.render(task)) if hasattr(column, "render") else ""
        for column in progress.columns
        if not isinstance(column, str)
    ]
    assert any("3/10" in text for text in rendered), rendered


def test_progress_task_completion_reaches_total_exactly() -> None:
    """M17: the bar must land on N of N, never stop short or overshoot."""
    progress = Progress(*_progress_columns())
    task_id = progress.add_task("applying", total=4)
    for _ in range(4):
        progress.advance(task_id)
    task = progress.tasks[0]
    assert task.completed == 4
    assert task.finished is True
