"""Drain one user-lane item: skip if fresh, else run stems or lyrics.

One track per claim so remaining pending items stay reorderable. A late
``finish_item(..., claimed_by=)`` cannot stamp done over cancelled (#1586).

-Claude
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from apps.analysis import queue_store, queue_user
from apps.analysis.queue_user_lanes import SKIP_UP_TO_DATE, USER_BATCH_IDS
from apps.lyrics.cache import LyricsUnavailableError
from apps.lyrics.service import LyricsService
from apps.shared.events import publish

log = logging.getLogger("apps.analysis.queue_user_runner")

ExecuteFn = Callable[[str], None]


def _publish(stable_id: str) -> None:
    publish("library.changed", {"kind": "library_jobs", "ids": [stable_id]})


def run_claimed(
    conn: sqlite3.Connection,
    item: queue_user.UserJobItem,
    *,
    stems_root: Path,
    data_dir: Path,
    execute_stems: ExecuteFn | None = None,
    execute_lyrics: ExecuteFn | None = None,
) -> str:
    """Finish one claimed item. Returns the terminal state written."""
    batch_id = USER_BATCH_IDS[item.lane]
    runner_id = item.runner_id or ""
    if queue_user.artifact_is_fresh(
        item.lane, item.stable_id, stems_root=stems_root, data_dir=data_dir
    ):
        moved = queue_store.finish_item(
            conn,
            batch_id=batch_id,
            stable_id=item.stable_id,
            lane=item.lane,
            state=queue_store.ITEM_SKIPPED,
            reason=SKIP_UP_TO_DATE,
            claimed_by=runner_id,
        )
        conn.commit()
        if moved:
            _publish(item.stable_id)
        return queue_store.ITEM_SKIPPED if moved else queue_store.ITEM_CANCELLED
    try:
        if item.lane == "stems":
            (execute_stems or _default_stems)(item.stable_id)
        else:
            (execute_lyrics or _default_lyrics(data_dir))(item.stable_id)
        state = queue_store.ITEM_DONE
        reason = None
    except LyricsUnavailableError as exc:
        state = queue_store.ITEM_FAILED
        reason = str(exc)
    except Exception as exc:
        state = queue_store.ITEM_FAILED
        reason = str(exc)
        log.exception("user-lane %s/%s failed", item.lane, item.stable_id)
    moved = queue_store.finish_item(
        conn,
        batch_id=batch_id,
        stable_id=item.stable_id,
        lane=item.lane,
        state=state,
        reason=reason,
        claimed_by=runner_id,
    )
    conn.commit()
    if moved:
        _publish(item.stable_id)
    return state if moved else queue_store.ITEM_CANCELLED


def tick_lane(
    conn: sqlite3.Connection,
    lane: str,
    *,
    runner_id: str,
    stems_root: Path,
    data_dir: Path,
    execute_stems: ExecuteFn | None = None,
    execute_lyrics: ExecuteFn | None = None,
) -> str:
    """Claim at most one item for ``lane`` and run it. Returns a named outcome."""
    claimed = queue_user.claim_next_for_lane(conn, lane, runner_id=runner_id)
    if claimed is None:
        return "empty"
    _publish(claimed.stable_id)
    run_claimed(
        conn,
        claimed,
        stems_root=stems_root,
        data_dir=data_dir,
        execute_stems=execute_stems,
        execute_lyrics=execute_lyrics,
    )
    return "ran"


def _default_stems(stable_id: str) -> None:
    """Spawn the existing one-id stems worker. Out of process on purpose."""
    from apps.stems.job import build_argv

    argv = build_argv({"stable_ids": [stable_id]})
    import subprocess

    subprocess.run(argv, check=True)


def _default_lyrics(data_dir: Path) -> ExecuteFn:
    service = LyricsService(data_dir)

    def _run(stable_id: str) -> None:
        service.fetch_stable_id(stable_id)

    return _run
