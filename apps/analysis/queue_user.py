"""User-ordered stems and lyrics jobs on the persistent analysis queue.

Issue #1865 / PERFBATCH-05. One table, two standing batches, two lanes.
Order is ``position``, not ``enqueued_at``. Analysis ``claim_next(batch_id)``
is untouched so #1586 tests keep their meaning.

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from apps.lyrics import cache as lyrics_cache
from apps.lyrics import fetch_verdicts
from apps.lyrics.service import vocals_sha256_for_track
from apps.stems.selection import has_bundle

from . import queue_store
from ._value_check import require_allowed_value
from .queue import QueueError
from .queue_user_lanes import (
    MAX_ENQUEUE_IDS,
    SKIP_DETAIL,
    SKIP_UP_TO_DATE,
    USER_BACKENDS,
    USER_BATCH_IDS,
    USER_JOB_LANES,
)

Placement = Literal["next", "tail"]
ACTIVE_STATES: tuple[str, ...] = (queue_store.ITEM_PENDING, queue_store.ITEM_RUNNING)
SETTLED_STATES: tuple[str, ...] = (
    queue_store.ITEM_DONE,
    queue_store.ITEM_SKIPPED,
    queue_store.ITEM_FAILED,
    queue_store.ITEM_CANCELLED,
)
_ITEM_COLS = (
    "batch_id, stable_id, lane, backend, file_path, duration_s, "
    "predicted_peak_mb, state, reason, attempts, enqueued_at, started_at, "
    "finished_at, runner_id, position"
)


class UserJobConflict(QueueError):
    """The item is not in a state that accepts this mutation."""


@dataclass(frozen=True)
class UserJobItem:
    """One stems or lyrics job in a standing user-lane batch."""

    batch_id: str
    stable_id: str
    lane: str
    state: str
    reason: str | None
    position: int
    attempts: int
    enqueued_at: str
    started_at: str | None
    finished_at: str | None
    runner_id: str | None

    @property
    def detail(self) -> str | None:
        if self.state == queue_store.ITEM_SKIPPED and self.reason == SKIP_UP_TO_DATE:
            return SKIP_DETAIL
        return self.reason


@dataclass(frozen=True)
class EnqueueUserResult:
    lane: str
    items: tuple[UserJobItem, ...]
    already_running: tuple[str, ...]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _item_from_row(row: Sequence[object]) -> UserJobItem:
    return UserJobItem(
        batch_id=str(row[0]),
        stable_id=str(row[1]),
        lane=str(row[2]),
        state=str(row[7]),
        reason=str(row[8]) if row[8] is not None else None,
        attempts=int(row[9]),
        enqueued_at=str(row[10]),
        started_at=str(row[11]) if row[11] is not None else None,
        finished_at=str(row[12]) if row[12] is not None else None,
        runner_id=str(row[13]) if row[13] is not None else None,
        position=int(row[14] or 0),
    )


def require_lane(lane: str) -> str:
    return require_allowed_value(
        lane, USER_JOB_LANES, "user lane", "lanes", QueueError
    )


def ensure_user_schema(conn: sqlite3.Connection) -> None:
    """Queue tables, ``position`` column, and the two standing batches."""
    queue_store.ensure_queue_tables(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(analysis_queue_item)")}
    if "position" not in cols:
        conn.execute(
            "ALTER TABLE analysis_queue_item "
            "ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
        )
    now = _now_iso()
    model = '{"source":"user-lane","backend":"user","producer_version":"1"}'
    for lane, batch_id in USER_BATCH_IDS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO analysis_queue_batch
                (batch_id, created_at, updated_at, state, workers, band,
                 memory_model, note)
            VALUES (?, ?, ?, ?, 1, 'user', ?, ?)
            """,
            (batch_id, now, now, queue_store.BATCH_QUEUED, model, f"user lane {lane}"),
        )
    conn.commit()


def _get_row(
    conn: sqlite3.Connection, batch_id: str, stable_id: str, lane: str
) -> UserJobItem | None:
    row = conn.execute(
        f"SELECT {_ITEM_COLS} FROM analysis_queue_item "
        "WHERE batch_id = ? AND stable_id = ? AND lane = ?",
        (batch_id, stable_id, lane),
    ).fetchone()
    return _item_from_row(row) if row is not None else None


def _live_tracks(
    conn: sqlite3.Connection, stable_ids: Sequence[str]
) -> dict[str, tuple[str | None, int | None]]:
    wanted = list(dict.fromkeys(stable_ids))
    found: dict[str, tuple[str | None, int | None]] = {}
    for offset in range(0, len(wanted), 500):
        chunk = wanted[offset : offset + 500]
        placeholders = ",".join("?" * len(chunk))
        for sid, path, duration_ms in conn.execute(
            f"SELECT stable_id, file_path, duration_ms FROM tracks "
            f"WHERE stable_id IN ({placeholders}) AND deleted_at IS NULL",
            chunk,
        ):
            found[str(sid)] = (
                str(path) if path else None,
                int(duration_ms) if duration_ms is not None else None,
            )
    missing = [sid for sid in wanted if sid not in found]
    if missing:
        raise QueueError(
            f"{len(missing)} stable_id(s) are not a live track "
            f"(unknown or deleted): {missing[:5]}"
        )
    return found


def artifact_is_fresh(
    lane: str,
    stable_id: str,
    *,
    stems_root: Path,
    data_dir: Path,
) -> bool:
    if lane == "stems":
        return has_bundle(stable_id, stems_root)
    path = lyrics_cache.cache_path(data_dir, stable_id)
    loaded = lyrics_cache.load(path)
    if loaded is not None and loaded.stable_id == stable_id:
        return True
    verdict = fetch_verdicts.load_verdict(data_dir, stable_id)
    if verdict is None:
        return False
    return fetch_verdicts.is_terminal_fresh(
        verdict, vocals_sha256_for_track(data_dir, stable_id)
    )


def _upsert(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    stable_id: str,
    lane: str,
    file_path: str,
    duration_s: float | None,
    state: str,
    reason: str | None,
    position: int,
) -> None:
    now = _now_iso()
    settled = state in SETTLED_STATES
    conn.execute(
        f"""
        INSERT INTO analysis_queue_item ({_ITEM_COLS})
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, NULL, ?, NULL, ?)
        ON CONFLICT(batch_id, stable_id, lane) DO UPDATE SET
            file_path = excluded.file_path,
            duration_s = excluded.duration_s,
            state = excluded.state,
            reason = excluded.reason,
            position = excluded.position,
            started_at = NULL,
            finished_at = excluded.finished_at,
            runner_id = NULL
        """,
        (
            batch_id,
            stable_id,
            lane,
            USER_BACKENDS[lane],
            file_path,
            duration_s,
            state,
            reason,
            now,
            now if settled else None,
            position,
        ),
    )


def _pending_ids(conn: sqlite3.Connection, batch_id: str, lane: str) -> list[str]:
    rows = conn.execute(
        "SELECT stable_id FROM analysis_queue_item "
        "WHERE batch_id = ? AND lane = ? AND state = ? "
        "ORDER BY position ASC, enqueued_at ASC, stable_id ASC",
        (batch_id, lane, queue_store.ITEM_PENDING),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _resequence(
    conn: sqlite3.Connection, batch_id: str, lane: str, ordered: Sequence[str]
) -> None:
    for index, sid in enumerate(ordered, start=1):
        conn.execute(
            "UPDATE analysis_queue_item SET position = ? "
            "WHERE batch_id = ? AND stable_id = ? AND lane = ? AND state = ?",
            (index, batch_id, sid, lane, queue_store.ITEM_PENDING),
        )


def _plan_one(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    lane: str,
    sid: str,
    tracks: dict[str, tuple[str | None, int | None]],
    stems_root: Path,
    data_dir: Path,
) -> str:
    existing = _get_row(conn, batch_id, sid, lane)
    path, duration_ms = tracks[sid]
    duration_s = None if duration_ms is None else duration_ms / 1000.0
    file_path = path or ""
    if existing is not None and existing.state == queue_store.ITEM_RUNNING:
        return "running"
    if artifact_is_fresh(lane, sid, stems_root=stems_root, data_dir=data_dir):
        _upsert(
            conn,
            batch_id=batch_id,
            stable_id=sid,
            lane=lane,
            file_path=file_path,
            duration_s=duration_s,
            state=queue_store.ITEM_SKIPPED,
            reason=SKIP_UP_TO_DATE,
            position=0,
        )
        return "skipped"
    _upsert(
        conn,
        batch_id=batch_id,
        stable_id=sid,
        lane=lane,
        file_path=file_path,
        duration_s=duration_s,
        state=queue_store.ITEM_PENDING,
        reason=None,
        position=0,
    )
    return "pending"


def enqueue_next(
    conn: sqlite3.Connection,
    *,
    lane: str,
    stable_ids: Sequence[str],
    placement: Placement = "next",
    stems_root: Path,
    data_dir: Path,
) -> EnqueueUserResult:
    """Put selection at the head (or tail) of that lane's pending list."""
    require_lane(lane)
    if not stable_ids:
        raise QueueError("enqueue needs at least one stable_id")
    if len(stable_ids) > MAX_ENQUEUE_IDS:
        raise QueueError(
            f"enqueue capped at {MAX_ENQUEUE_IDS} stable_ids, got {len(stable_ids)}"
        )
    ordered = list(dict.fromkeys(stable_ids))
    ensure_user_schema(conn)
    batch_id = USER_BATCH_IDS[lane]
    conn.execute("BEGIN IMMEDIATE")
    try:
        tracks = _live_tracks(conn, ordered)
        already_running: list[str] = []
        result_ids: list[str] = []
        head_pending: list[str] = []
        for sid in ordered:
            kind = _plan_one(
                conn,
                batch_id=batch_id,
                lane=lane,
                sid=sid,
                tracks=tracks,
                stems_root=stems_root,
                data_dir=data_dir,
            )
            result_ids.append(sid)
            if kind == "running":
                already_running.append(sid)
            elif kind == "pending":
                head_pending.append(sid)
        previous = [sid for sid in _pending_ids(conn, batch_id, lane) if sid not in head_pending]
        if placement == "tail":
            _resequence(conn, batch_id, lane, previous + head_pending)
        else:
            _resequence(conn, batch_id, lane, head_pending + previous)
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    items = tuple(
        item
        for sid in result_ids
        if (item := _get_row(conn, batch_id, sid, lane)) is not None
    )
    return EnqueueUserResult(
        lane=lane, items=items, already_running=tuple(already_running)
    )


def list_lane(
    conn: sqlite3.Connection,
    lane: str,
    *,
    include_settled: bool = False,
) -> tuple[UserJobItem, ...]:
    require_lane(lane)
    ensure_user_schema(conn)
    batch_id = USER_BATCH_IDS[lane]
    states = ACTIVE_STATES + (SETTLED_STATES if include_settled else ())
    placeholders = ",".join("?" * len(states))
    rows = conn.execute(
        f"SELECT {_ITEM_COLS} FROM analysis_queue_item "
        f"WHERE batch_id = ? AND lane = ? AND state IN ({placeholders}) "
        "ORDER BY CASE WHEN state IN ('pending','running') THEN 0 ELSE 1 END, "
        "position ASC, enqueued_at ASC, stable_id ASC",
        (batch_id, lane, *states),
    ).fetchall()
    return tuple(_item_from_row(r) for r in rows)


def counts_for_lane(conn: sqlite3.Connection, lane: str) -> dict[str, int]:
    require_lane(lane)
    ensure_user_schema(conn)
    out = {
        s: 0
        for s in (
            queue_store.ITEM_PENDING,
            queue_store.ITEM_RUNNING,
            queue_store.ITEM_DONE,
            queue_store.ITEM_SKIPPED,
            queue_store.ITEM_FAILED,
            queue_store.ITEM_CANCELLED,
        )
    }
    for state, n in conn.execute(
        "SELECT state, COUNT(*) FROM analysis_queue_item "
        "WHERE batch_id = ? AND lane = ? GROUP BY state",
        (USER_BATCH_IDS[lane], lane),
    ):
        if state in out:
            out[state] = int(n)
    return out


def _loaded_item(
    conn: sqlite3.Connection, batch_id: str, lane: str, stable_id: str
) -> UserJobItem:
    item = _get_row(conn, batch_id, stable_id, lane)
    if item is None:
        raise QueueError(f"no {lane} job for {stable_id}")
    return item


def _require_pending(item: UserJobItem) -> None:
    if item.state != queue_store.ITEM_PENDING:
        raise UserJobConflict(
            f"{item.lane}/{item.stable_id} is {item.state}, not pending"
        )


def _require_active(item: UserJobItem) -> None:
    if item.state in SETTLED_STATES:
        raise UserJobConflict(
            f"{item.lane}/{item.stable_id} is already {item.state}"
        )


def _insert_before(
    pending: list[str],
    stable_id: str,
    before_stable_id: str | None,
    lane: str,
) -> None:
    if before_stable_id is None:
        pending.append(stable_id)
        return
    if before_stable_id not in pending:
        raise QueueError(
            f"before_stable_id {before_stable_id!r} is not a pending {lane} job"
        )
    pending.insert(pending.index(before_stable_id), stable_id)


def reorder_item(
    conn: sqlite3.Connection,
    *,
    lane: str,
    stable_id: str,
    before_stable_id: str | None,
) -> UserJobItem:
    require_lane(lane)
    ensure_user_schema(conn)
    batch_id = USER_BATCH_IDS[lane]
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = _loaded_item(conn, batch_id, lane, stable_id)
        _require_pending(item)
        pending = _pending_ids(conn, batch_id, lane)
        pending.remove(stable_id)
        _insert_before(pending, stable_id, before_stable_id, lane)
        _resequence(conn, batch_id, lane, pending)
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    moved = _get_row(conn, batch_id, stable_id, lane)
    assert moved is not None
    return moved


def cancel_item(
    conn: sqlite3.Connection, *, lane: str, stable_id: str
) -> UserJobItem:
    require_lane(lane)
    ensure_user_schema(conn)
    batch_id = USER_BATCH_IDS[lane]
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = _loaded_item(conn, batch_id, lane, stable_id)
        _require_active(item)
        conn.execute(
            "UPDATE analysis_queue_item SET state = ?, runner_id = NULL, "
            "finished_at = ?, reason = NULL WHERE batch_id = ? AND stable_id = ? "
            "AND lane = ? AND state IN (?, ?)",
            (
                queue_store.ITEM_CANCELLED,
                _now_iso(),
                batch_id,
                stable_id,
                lane,
                queue_store.ITEM_PENDING,
                queue_store.ITEM_RUNNING,
            ),
        )
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    cancelled = _get_row(conn, batch_id, stable_id, lane)
    assert cancelled is not None
    return cancelled


def claim_next_for_lane(
    conn: sqlite3.Connection, lane: str, *, runner_id: str
) -> UserJobItem | None:
    """Take the next pending item of this lane, ordered by position."""
    require_lane(lane)
    ensure_user_schema(conn)
    batch_id = USER_BATCH_IDS[lane]
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLS} FROM analysis_queue_item "
            "WHERE batch_id = ? AND lane = ? AND state = ? "
            "ORDER BY position ASC, enqueued_at ASC, stable_id ASC LIMIT 1",
            (batch_id, lane, queue_store.ITEM_PENDING),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        item = _item_from_row(row)
        now = _now_iso()
        changed = conn.execute(
            "UPDATE analysis_queue_item SET state = ?, started_at = ?, "
            "runner_id = ?, attempts = attempts + 1 "
            "WHERE batch_id = ? AND stable_id = ? AND lane = ? AND state = ?",
            (
                queue_store.ITEM_RUNNING,
                now,
                runner_id,
                batch_id,
                item.stable_id,
                lane,
                queue_store.ITEM_PENDING,
            ),
        ).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if changed != 1:
        raise RuntimeError(
            f"claim of {item.stable_id}/{lane} updated {changed} rows"
        )
    claimed = _get_row(conn, batch_id, item.stable_id, lane)
    assert claimed is not None
    return claimed
