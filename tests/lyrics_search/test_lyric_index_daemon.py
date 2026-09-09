"""Daemon wiring of the lyric-index background job (LYRICS-03, #1343).

The engine's reconcile batch is pure and thread-free; this module proves the
webui daemon actually RUNS it: create_app arms it only when asked, the
lifespan starts and joins a real thread, and the thread's job indexes a real
lyrics cache on disk while nobody is using the UI.

Same rule as tests/webui/analysis: drive the PRODUCTION wiring
(``apps.webui.server.app.create_app`` + the watcher it builds in its
lifespan), never a hand-built stand-in.

Regression lines:
  - if create_app arms the lyric index by default then broken
  - if the daemon lifespan does not run and join a real lyric-index thread then broken
  - if the real thread never indexes the real cache then broken
  - if a raising tick leaves last_outcome reporting the previous success then broken
  - if a frozen bulk-removal guard is not logged with its exact recovery command then broken
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.search_index import (
    LyricsCacheUnavailable,
    count_documents,
    index_batch,
    index_path,
    open_write,
)
from apps.webui.server import lyric_index_autostart


def test_create_app_leaves_the_lyric_index_disarmed() -> None:
    """Only the real daemon entry point arms the background job."""
    from apps.webui.server.app import create_app

    assert create_app().state.lyric_index.enabled is False


def _write(data_dir, stable_id: str) -> None:
    lyrics = Lyrics(
        stable_id=stable_id,
        source="lrclib",
        lines=(LyricLine(0, f"{stable_id} dancing in the moonlight"),),
    )
    write(cache_path(data_dir, stable_id), lyrics)


def _index_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.name == lyric_index_autostart.THREAD_NAME and t.is_alive()
    ]


def test_lifespan_runs_and_joins_a_real_lyric_index_thread(tmp_path: Path) -> None:
    """A daemon that never starts the thread would never index. The real thread
    runs over a real cache and joins when the lifespan closes."""
    from apps.webui.server import app as app_mod

    data_dir = tmp_path / "data"
    state_db_path = data_dir / "state" / "state.db"
    _write(data_dir, "a")

    armed = app_mod.create_app(
        state_db_path=str(state_db_path),
        mount_frontend=False,
        port=18736,
        frontend_port=19736,
        lyric_index=True,
    )
    armed.state.lyric_index.interval_s = 0.05

    with TestClient(armed) as client:
        assert client.get("/api/v1/health").status_code == 200
        deadline = time.time() + 10
        while (
            armed.state.lyric_index.last_outcome not in ("indexed", "idle")
            and time.time() < deadline
        ):
            time.sleep(0.05)
        assert armed.state.lyric_index.last_outcome in ("indexed", "idle"), (
            "the lifespan loop never reported a real outcome"
        )
        assert _index_threads(), "the lifespan did not start the lyric-index loop"
        conn = open_write(index_path(data_dir))
        try:
            assert count_documents(conn) == 1, "the thread never indexed the cache"
        finally:
            conn.close()

    assert not _index_threads(), "the lifespan did not join the lyric-index loop"


def _shrink_past_the_bound(data_dir: Path) -> None:
    """Index 4 real entries, then delete 3 of the 4 cache files - a 75%
    removal, over MAX_REMOVAL_FRACTION (50%), so the next batch raises."""
    for stable_id in ("a", "b", "c", "d"):
        _write(data_dir, stable_id)
    for _ in range(1000):
        batch = index_batch(data_dir, max_docs=100)
        if batch.done:
            break
    else:
        raise AssertionError("initial index did not drain")
    for stable_id in ("a", "b", "c"):
        cache_path(data_dir, stable_id).unlink()


def test_a_raising_tick_sets_last_outcome_to_failed_before_freezing(
    tmp_path: Path,
) -> None:
    """Issue #1343 round-3 review (three threads on this line: P3
    PRRT_kwDOSEvNd86fwSZO plus two earlier P2 duplicates of the same
    finding): a tick that raises must never leave last_outcome reporting the
    previous success. last_outcome becomes FAILED_OUTCOME before the
    exception propagates, so a status route reading it right after a raise
    never claims the index is still healthy."""
    data_dir = tmp_path / "data"
    state = lyric_index_autostart.build(enabled=True)
    watcher = lyric_index_autostart.LyricIndexWatcher(
        state, data_dir=data_dir, activity_fn=lambda: False
    )
    _write(data_dir, "a")

    assert watcher.tick() == "indexed"
    assert watcher.state.last_outcome == "indexed"

    _shrink_past_the_bound(data_dir)

    with pytest.raises(LyricsCacheUnavailable):
        watcher.tick()
    assert watcher.state.last_outcome == lyric_index_autostart.FAILED_OUTCOME, (
        "a raising tick must not leave last_outcome at its previous success"
    )


def test_run_loop_logs_the_exact_recovery_command_when_frozen(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A frozen bulk-removal guard must be loud - not blended into the
    generic 'tick failed' warning every other exception gets - and must name
    the exact command an operator runs to recover, since the watcher never
    force-rebuilds on its own."""
    data_dir = tmp_path / "data"
    _shrink_past_the_bound(data_dir)

    state = lyric_index_autostart.build(enabled=True, interval_s=60.0)
    watcher = lyric_index_autostart.LyricIndexWatcher(
        state, data_dir=data_dir, activity_fn=lambda: False
    )

    def _stop_after_one_wait(_timeout: float | None = None) -> bool:
        watcher._stop.set()
        return True

    watcher._stop.wait = _stop_after_one_wait  # type: ignore[method-assign, assignment]

    with caplog.at_level("ERROR", logger=lyric_index_autostart.log.name):
        watcher._run()

    assert any(
        "python -m apps.lyrics index --force-rebuild --data-dir" in record.message
        for record in caplog.records
    ), f"no recovery-command log line found in {[r.message for r in caplog.records]}"
