"""Pausable reconcile job over the lyric-search index (LYRICS-03, #1343).

The job is the one place "yield to the UI" lives. A tick is one poll: it
checks the activity probe before doing any indexing, so the moment the UI is
active the very next tick reports ``paused`` and touches nothing - indexing
pauses within one poll. When idle it runs exactly one bounded batch and
hands control back so the caller can poll again.

Regression lines:
  - if a tick indexes anything while the UI is active then broken
  - if a tick does no work while the UI is idle and work is pending then broken
  - if a tick reports idle while work is still pending then broken
  - if a stop request is ignored and a batch still runs then broken
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.index_job import LyricIndexJob
from apps.lyrics.search_index import index_path, open_write, read_meta, search


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _write(data_dir: Path, stable_id: str, text: str) -> None:
    lyrics = Lyrics(
        stable_id=stable_id,
        source="lrclib",
        lines=(LyricLine(0, text),),
    )
    write(cache_path(data_dir, stable_id), lyrics)


def test_tick_pauses_within_one_poll_when_the_ui_is_active(data_dir: Path) -> None:
    """If the UI is active then a tick does zero indexing and reports paused."""
    _write(data_dir, "a", "dancing in the moonlight")

    job = LyricIndexJob(data_dir, activity=lambda: True)

    assert job.tick() == "paused"
    # No batch ran: not even an empty index file was created.
    assert index_path(data_dir).exists() is False


def test_tick_indexes_when_idle_and_pauses_once_activity_returns(
    data_dir: Path,
) -> None:
    """If the UI is idle then a tick drains one bounded batch; if the UI turns
    active again the next tick pauses instead of continuing."""
    for sid in ("a", "b", "c"):
        _write(data_dir, sid, f"{sid} lyrics mention the moonlight")
    active = {"value": False}

    job = LyricIndexJob(data_dir, max_docs=1, activity=lambda: active["value"])

    assert job.tick() == "indexed"
    active["value"] = True
    assert job.tick() == "paused"
    assert job.last_outcome == "paused"

    active["value"] = False
    # One indexed tick already committed 'a'; the pause happened between
    # batches, and once the UI is idle again the job drains the rest.
    while job.tick() == "indexed":
        pass
    assert job.last_outcome == "idle"

    conn = open_write(index_path(data_dir))
    try:
        assert sorted(search(conn, "moonlight", limit=10)[0]) == ["a", "b", "c"]
    finally:
        conn.close()


def test_tick_drains_to_idle_and_stays_idle(data_dir: Path) -> None:
    """If there is no work left a tick reports idle and keeps reporting it."""
    _write(data_dir, "a", "dancing in the moonlight")

    job = LyricIndexJob(data_dir, max_docs=1, activity=lambda: False)

    assert job.tick() == "indexed"
    assert job.tick() == "idle"
    assert job.tick() == "idle"


def test_tick_honours_a_stop_request_before_starting_work(data_dir: Path) -> None:
    """If stopping was requested then a tick reports stopping and starts nothing."""
    _write(data_dir, "a", "dancing in the moonlight")

    job = LyricIndexJob(
        data_dir,
        max_docs=1,
        activity=lambda: False,
        should_stop=lambda: True,
    )

    assert job.tick() == "stopping"
    assert index_path(data_dir).exists() is False


def test_tick_checkpoints_last_committed_ms_as_wall_clock(data_dir: Path) -> None:
    """Issue #1343 round-3 review (thread PRRT_kwDOSEvNd86fvm68, fixed in
    ae17a3be3): the review comment was raised against a monotonic-clock
    checkpoint that a prior commit already replaced with ``wall_clock``
    (default ``time.time``) at the ``index_batch`` layer, but the only test
    covering it called ``index_batch`` directly. Pin the same invariant at
    the layer the daemon actually drives (``LyricIndexJob.tick``), so a
    regression that reintroduces a monotonic source anywhere between the
    watcher and the checkpoint write is caught even if ``index_batch`` is
    never called from a test directly."""
    _write(data_dir, "a", "dancing in the moonlight")

    job = LyricIndexJob(data_dir, activity=lambda: False)

    before_ms = time.time() * 1000
    assert job.tick() == "indexed"
    after_ms = time.time() * 1000

    conn = open_write(index_path(data_dir))
    try:
        meta = read_meta(conn)
        assert meta is not None
        last_committed_ms = meta["last_committed_ms"]
        assert last_committed_ms is not None
        # A monotonic-since-boot value would be many orders of magnitude
        # smaller than an epoch-ms window a couple of seconds wide.
        assert before_ms - 2000 <= last_committed_ms <= after_ms + 2000
    finally:
        conn.close()


def test_disabled_job_reports_disabled(data_dir: Path) -> None:
    """If the job is disabled it says so and never indexes."""
    _write(data_dir, "a", "dancing in the moonlight")

    job = LyricIndexJob(data_dir, enabled=False, activity=lambda: False)

    assert job.tick() == "disabled"
    assert index_path(data_dir).exists() is False
