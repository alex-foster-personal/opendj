"""Drain one user-lane item: skip if fresh, else run stems or lyrics.

One track per claim so remaining pending items stay reorderable. A late
``finish_item(..., claimed_by=)`` cannot stamp done over cancelled (#1586).

-Claude
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from apps.analysis import queue_store, queue_user
from apps.analysis.queue_user_lanes import SKIP_UP_TO_DATE, USER_BATCH_IDS
from apps.lyrics.asr_source import LyricsAsrFetchError
from apps.lyrics.service import FetchResult, LyricsFetchService
from apps.shared.events import publish

log = logging.getLogger("apps.analysis.queue_user_runner")

ExecuteFn = Callable[[str], None]

LIBRARY_JOBS_RUNNER_ENV: str = "MUSIC_DJ_LIBRARY_JOBS_RUNNER"
LIBRARY_JOBS_RUNNER_VALUES: tuple[str, ...] = ("live", "dry")
LIBRARY_JOBS_DRY_HOLD_ENV: str = "MUSIC_DJ_LIBRARY_JOBS_DRY_HOLD_S"
DEFAULT_DRY_HOLD_S: float = 2.5


def runner_from_environ(environ: Mapping[str, str] | None = None) -> str:
    """Fail-fast runner mode. CI and the library-jobs e2e set ``dry``."""
    mapping = environ if environ is not None else os.environ
    raw = mapping.get(LIBRARY_JOBS_RUNNER_ENV, "live").strip().lower()
    if raw not in LIBRARY_JOBS_RUNNER_VALUES:
        raise ValueError(
            f"{LIBRARY_JOBS_RUNNER_ENV}={raw!r} is not a member of {LIBRARY_JOBS_RUNNER_VALUES}"
        )
    return raw


def dry_hold_s_from_environ(environ: Mapping[str, str] | None = None) -> float:
    mapping = environ if environ is not None else os.environ
    raw = mapping.get(LIBRARY_JOBS_DRY_HOLD_ENV, str(DEFAULT_DRY_HOLD_S)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{LIBRARY_JOBS_DRY_HOLD_ENV}={raw!r} is not a valid float") from None
    if value < 0:
        raise ValueError(f"{LIBRARY_JOBS_DRY_HOLD_ENV}={raw!r} must be non-negative")
    return value


def _dry_execute(hold_s: float) -> ExecuteFn:
    def _run(_stable_id: str) -> None:
        time.sleep(hold_s)

    return _run


def _resolve_execute_stems(execute_stems: ExecuteFn | None) -> ExecuteFn:
    if execute_stems is not None:
        return execute_stems
    if runner_from_environ() == "dry":
        return _dry_execute(dry_hold_s_from_environ())
    return _default_stems


def _resolve_execute_lyrics(
    data_dir: Path, execute_lyrics: ExecuteFn | None
) -> ExecuteFn:
    if execute_lyrics is not None:
        return execute_lyrics
    if runner_from_environ() == "dry":
        return _dry_execute(dry_hold_s_from_environ())
    return _default_lyrics(data_dir)


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
    stems_fn = _resolve_execute_stems(execute_stems)
    lyrics_fn = _resolve_execute_lyrics(data_dir, execute_lyrics)
    try:
        if item.lane == "stems":
            stems_fn(item.stable_id)
            state = queue_store.ITEM_DONE
            reason = None
        else:
            state, reason = _finish_lyrics(lyrics_fn, item.stable_id)
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


def _finish_lyrics(lyrics_fn: ExecuteFn, stable_id: str) -> tuple[str, str | None]:
    result = lyrics_fn(stable_id)
    if isinstance(result, FetchResult):
        return queue_store.ITEM_DONE, result.outcome
    return queue_store.ITEM_DONE, None


def _default_lyrics(data_dir: Path) -> ExecuteFn:
    service = LyricsFetchService(data_dir)

    def _run(stable_id: str) -> FetchResult:
        try:
            return service.fetch_or_resolve_stable_id(stable_id)
        except LyricsAsrFetchError as exc:
            raise RuntimeError(exc.message) from exc

    return _run
