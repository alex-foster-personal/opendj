"""Drain the ``genre`` user-job lane: claim a small batch, infer, settle every item.

Unlike stems and lyrics (one track per claim, minutes each), a genre job is a
few network calls, so one tick claims up to :data:`GENRE_BATCH` items and runs
them concurrently through :func:`apps.genre_infer.pipeline.infer_tracks`.

Settling: ``done`` (a fused genre stored), ``skipped`` with ``up_to_date`` (the
stored finding came from the same request), ``skipped`` with a named reason (no
artist or title, or ``unavailable: <KEY> is not set``), ``failed`` with the
reason (a call or parse failure). A missing key never reads as done.

Ingestion (GENRE-03): when ``auto_infer`` is on in the genre config and the lane
has nothing pending, a tick enqueues up to :data:`AUTO_BATCH` live tracks with
local audio and no fused finding and no earlier genre job, so newly imported
tracks are tagged without anyone asking. The scan is rate-limited, and a failed
or unavailable track is retried once :data:`AUTO_RETRY_AFTER_S` has passed.

-Claude
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.analysis import queue_store, queue_user
from apps.analysis.queue_user_lanes import USER_BATCH_IDS
from apps.genre_infer import findings, pipeline, providers, service, tag_write
from apps.shared.events import publish

log = logging.getLogger("apps.analysis.queue_user_genre")

LANE = "genre"
GENRE_BATCH = 8
AUTO_BATCH = 50
AUTO_RESCAN_S = 300.0
AUTO_RETRY_AFTER_S = 6 * 3600.0
_last_empty_scan: dict[str, float] = {}
_released: set[str] = set()

_STATE_BY_OUTCOME = {
    pipeline.OUTCOME_DONE: queue_store.ITEM_DONE,
    pipeline.OUTCOME_UP_TO_DATE: queue_store.ITEM_SKIPPED,
    pipeline.OUTCOME_SKIPPED: queue_store.ITEM_SKIPPED,
    pipeline.OUTCOME_FAILED: queue_store.ITEM_FAILED,
    pipeline.OUTCOME_CANCELLED: queue_store.ITEM_CANCELLED,
}


def _finish(conn: sqlite3.Connection, item: queue_user.UserJobItem, state: str, reason: str | None) -> bool:
    """Settle a claimed item; False when it was cancelled (the claim no longer holds)."""
    return queue_store.finish_item(
        conn,
        batch_id=USER_BATCH_IDS[LANE],
        stable_id=item.stable_id,
        lane=LANE,
        state=state,
        reason=reason,
        claimed_by=item.runner_id or "",
    )


def _release_stale_claims(conn: sqlite3.Connection, runner_id: str) -> None:
    """Once per process: items left ``running`` by an earlier process go back to pending.

    The runner id carries the pid, so a running item with another runner id was
    claimed by an engine that exited mid-batch; nothing was stored for it.
    """
    key = f"{runner_id}@{conn.execute('PRAGMA database_list').fetchone()[2]}"
    if key in _released:
        return
    released = conn.execute(
        "UPDATE analysis_queue_item SET state = ?, runner_id = NULL, started_at = NULL "
        "WHERE batch_id = ? AND lane = ? AND state = ? AND (runner_id IS NULL OR runner_id != ?)",
        (queue_store.ITEM_PENDING, USER_BATCH_IDS[LANE], LANE, queue_store.ITEM_RUNNING, runner_id),
    ).rowcount
    conn.commit()
    _released.add(key)
    if released:
        log.info("genre lane: %d stale running item(s) returned to pending", released)


def _auto_enqueue(conn: sqlite3.Connection, *, data_dir: Path, stems_root: Path) -> int:
    """Enqueue untagged present tracks when auto-inference is on; returns how many."""
    findings.ensure_schema(conn)
    if not findings.read_config(conn).auto_infer or providers.missing_keys():
        return 0
    key = str(data_dir)
    if time.monotonic() - _last_empty_scan.get(key, -AUTO_RESCAN_S) < AUTO_RESCAN_S:
        return 0
    # Not re-queued: a job that is queued or running, and one that finished
    # within AUTO_RETRY_AFTER_S (so an outage is retried once it can have
    # recovered, never in a paying loop). A track with nothing to search for is
    # left out by its CURRENT metadata, so fixing its artist or title queues it.
    retry_cutoff = (datetime.now(UTC) - timedelta(seconds=AUTO_RETRY_AFTER_S)).isoformat().replace("+00:00", "Z")
    held = {
        r[0]
        for r in conn.execute(
            "SELECT stable_id FROM analysis_queue_item WHERE batch_id = ? AND lane = ? AND ("
            "state IN (?, ?) OR (finished_at IS NOT NULL AND finished_at > ?))",
            (USER_BATCH_IDS[LANE], LANE, queue_store.ITEM_PENDING, queue_store.ITEM_RUNNING, retry_cutoff),
        )
    }
    missing = pipeline.missing_fused_ids(conn, pipeline.eligible_ids(conn))
    todo = [sid for sid in pipeline.searchable_ids(conn, missing) if sid not in held]
    if not todo:
        _last_empty_scan[key] = time.monotonic()
        return 0
    queue_user.enqueue_next(
        conn, lane=LANE, stable_ids=todo[:AUTO_BATCH], placement="tail", stems_root=stems_root, data_dir=data_dir
    )
    return min(len(todo), AUTO_BATCH)


def _maybe_write_tags(conn: sqlite3.Connection, data_dir: Path, done: list[str]) -> None:
    """GENRE-05: write the chosen genre into each file when the config turns it on."""
    if not done or not findings.read_config(conn).write_file_tags:
        return
    for sid in done:
        try:
            service.write_chosen_tag(conn, data_dir=data_dir, stable_id=sid)
        except tag_write.TagWriteRefused as exc:
            log.warning("genre tag not written for %s: %s", sid, exc)
        except Exception:
            # One unreadable file must not stop the rest of the batch; the
            # genre itself is already stored, and the failure is logged in full.
            log.exception("genre tag write failed for %s", sid)


SKIP_DRY_RUNNER = "dry_runner: no network calls in dry mode"


def _reason(result: pipeline.TrackResult) -> str | None:
    return pipeline.OUTCOME_UP_TO_DATE if result.outcome == pipeline.OUTCOME_UP_TO_DATE else result.reason


def _settle(
    conn: sqlite3.Connection, claimed: list[queue_user.UserJobItem], summary: pipeline.RunSummary, settled: set[str]
) -> list[str]:
    """Settle the items the pipeline did not (no call was made); return the ids now done (commits)."""
    by_id = {r.stable_id: r for r in summary.results}
    for item in claimed:
        result = by_id[item.stable_id]
        if item.stable_id not in settled and result.outcome != pipeline.OUTCOME_CANCELLED:
            _finish(conn, item, _STATE_BY_OUTCOME[result.outcome], _reason(result))
    conn.commit()
    return [r.stable_id for r in summary.results if r.outcome == pipeline.OUTCOME_DONE]


def tick_genre(conn: sqlite3.Connection, *, runner_id: str, data_dir: Path, stems_root: Path, dry: bool) -> str:
    """Claim up to :data:`GENRE_BATCH` genre items and settle them. Returns a named outcome.

    ``dry`` (the library-jobs dry runner, CI and e2e) settles claimed items as
    skipped with :data:`SKIP_DRY_RUNNER` and never auto-enqueues: no genre is
    ever produced without a real call.
    """
    lock = pipeline.inference_lock(conn)
    # Another run (an HTTP infer) holds the database: claim nothing this tick.
    if not lock.acquire(blocking=False):
        return "busy"
    try:
        return _tick_locked(conn, runner_id=runner_id, data_dir=data_dir, stems_root=stems_root, dry=dry)
    finally:
        lock.release()


def _tick_locked(conn: sqlite3.Connection, *, runner_id: str, data_dir: Path, stems_root: Path, dry: bool) -> str:
    _release_stale_claims(conn, runner_id)
    claimed = _claim_batch(conn, runner_id)
    if not claimed and dry:
        return "empty"
    if not claimed:
        return "enqueued" if _auto_enqueue(conn, data_dir=data_dir, stems_root=stems_root) else "empty"
    if dry:
        _finish_all(conn, claimed, queue_store.ITEM_SKIPPED, SKIP_DRY_RUNNER)
        return "dry"
    try:
        keys = pipeline.required_keys()
    except providers.ProviderUnavailable as exc:
        _finish_all(conn, claimed, queue_store.ITEM_SKIPPED, f"unavailable: {exc}")
        publish("library.changed", {"kind": "library_jobs", "ids": [i.stable_id for i in claimed]})
        return "unavailable"
    return _run_batch(conn, claimed, keys, data_dir)


def _claim_batch(conn: sqlite3.Connection, runner_id: str) -> list[queue_user.UserJobItem]:
    claimed: list[queue_user.UserJobItem] = []
    while len(claimed) < GENRE_BATCH:
        item = queue_user.claim_next_for_lane(conn, LANE, runner_id=runner_id)
        if item is None:
            break
        claimed.append(item)
    return claimed


def _finish_all(conn: sqlite3.Connection, items: list[queue_user.UserJobItem], state: str, reason: str) -> None:
    for item in items:
        _finish(conn, item, state, reason)
    conn.commit()


def _run_batch(
    conn: sqlite3.Connection, claimed: list[queue_user.UserJobItem], keys: dict[str, str], data_dir: Path
) -> str:
    by_sid = {i.stable_id: i for i in claimed}
    settled: set[str] = set()

    def settle(result: pipeline.TrackResult) -> bool:
        settled.add(result.stable_id)
        return _finish(conn, by_sid[result.stable_id], _STATE_BY_OUTCOME[result.outcome], _reason(result))

    try:
        summary = pipeline.infer_tracks(conn, list(by_sid), keys=keys, settle=settle)
    except Exception as exc:
        log.exception("genre batch failed")
        _finish_all(conn, claimed, queue_store.ITEM_FAILED, f"{type(exc).__name__}: {exc}")
        return "failed"
    settled_done = _settle(conn, claimed, summary, settled)
    _maybe_write_tags(conn, data_dir, settled_done)
    publish("library.changed", {"kind": "tracks", "ids": [i.stable_id for i in claimed]})
    return "ran"


__all__ = ["AUTO_BATCH", "GENRE_BATCH", "LANE", "SKIP_DRY_RUNNER", "tick_genre"]
