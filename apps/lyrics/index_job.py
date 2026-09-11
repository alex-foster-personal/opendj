"""Pausable reconcile job over the checkpointed lyric index (Part 2 of #935).

The daemon (and the ``index`` CLI) drive this job instead of calling
``apps.lyrics.search_index.index_batch`` directly, so "yield to the UI" is
one behavior in one place. Each :meth:`tick` is one poll:

* it checks the activity probe FIRST and, if the UI is active, does zero
  indexing and reports ``paused`` - so indexing pauses within one poll of
  activity resuming;
* when idle it runs exactly ONE bounded batch (``index_batch``, capped by
  ``max_docs``), reports ``indexed``, and hands control back so the caller
  can poll again;
* a tick that finds nothing left to do reports ``idle``.

The batch is the only piece that touches the index, and it opens, commits
and closes the SQLite file itself, so a tick paused mid-run holds nothing in
RAM and every committed batch is durable on disk before the tick returns.

Outcomes are the vocabulary tests and log lines share:
``disabled`` / ``stopping`` / ``paused`` / ``indexed`` / ``idle``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from apps.lyrics import search_index

TICK_OUTCOMES: tuple[str, ...] = (
    "disabled",
    "stopping",
    "paused",
    "indexed",
    "idle",
)


class LyricIndexJob:
    """One poll loop's worth of state around the durable index batches.

    Thread-free on purpose: the webui daemon watcher
    (``apps/webui/server/lyric_index_autostart.py``) owns the thread and calls
    :meth:`tick` on its interval; this class owns only the decisions a tick
    makes, so pause/resume behavior is testable without threads.

    ``activity`` reports whether the UI is active right now (None means this
    job is headless and never yields). ``should_stop`` lets an owning thread
    request a clean stop between batches.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = True,
        max_docs: int = search_index.DEFAULT_BATCH_MAX_DOCS,
        activity: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        if max_docs <= 0:
            raise ValueError(f"max_docs must be > 0, got {max_docs}")
        self._data_dir = data_dir
        self._enabled = enabled
        self._max_docs = max_docs
        self._activity = activity
        self._should_stop = should_stop
        self.last_outcome: str | None = None
        self.last_batch: search_index.IndexBatch | None = None

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def tick(self) -> str:
        """One poll: pause for UI activity, else run exactly one bounded batch."""
        if not self._enabled:
            outcome = "disabled"
        elif self._should_stop is not None and self._should_stop():
            outcome = "stopping"
        elif self._activity is not None and self._activity():
            outcome = "paused"
        else:
            batch = search_index.index_batch(self._data_dir, max_docs=self._max_docs)
            self.last_batch = batch
            if batch.done and batch.added == 0 and batch.removed == 0:
                outcome = "idle"
            else:
                outcome = "indexed"
        self.last_outcome = outcome
        return outcome


__all__ = ["TICK_OUTCOMES", "LyricIndexJob"]
