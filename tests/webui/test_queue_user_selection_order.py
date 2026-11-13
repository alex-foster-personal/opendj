"""Caller selection order on the stems and lyrics user queue (issue #13218).

Spec ``(private path)``: order is ``position``, not
``enqueued_at``, and enqueue keeps the caller's selection order with an
explicit loop index, never ``WHERE stable_id IN (...)`` row order. Tracks are
seeded in an order that is neither the caller order nor sorted, so a result
that leaned on SQLite IN or rowid ordering would fail these tests.

-Claude
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.analysis import queue_user as user
from apps.analysis.store import open_conn

# Seed order (rowid order) differs from both sorted order and every caller order below.
_SEEDED = ["m", "z", "a", "q", "c", "x", "b"]


def _seed(db: Path, ids: list[str]) -> None:
    """Insert live tracks in the given order so rowid order is known."""
    conn = open_conn(db)
    for sid in ids:
        audio = db.parent / f"{sid}.wav"
        audio.write_bytes(b"\0")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, 180000, "
            "?, '2026-09-11T00:00:00Z', '2026-09-11T00:00:00Z')",
            (sid, sid, str(audio)),
        )
    conn.commit()
    conn.close()


def _conn(tmp: Path) -> sqlite3.Connection:
    """Open a seeded state db with the user-queue schema ready."""
    db = tmp / "state.db"
    _seed(db, _SEEDED)
    conn = open_conn(db)
    user.ensure_user_schema(conn)
    return conn


def _enqueue(conn: sqlite3.Connection, lane: str, ids: list[str], tmp: Path, placement: str) -> user.EnqueueUserResult:
    """Enqueue ids on a lane with no fresh artifacts on disk."""
    return user.enqueue_next(
        conn,
        lane=lane,
        stable_ids=ids,
        placement=placement,  # type: ignore[arg-type]
        stems_root=tmp / "stems",
        data_dir=tmp,
    )


def _pending(conn: sqlite3.Connection, lane: str) -> list[str]:
    """Pending ids of a lane in listed (position) order."""
    return [i.stable_id for i in user.list_lane(conn, lane) if i.state == "pending"]


def _drain(conn: sqlite3.Connection, lane: str) -> list[str]:
    """Claim every pending item of a lane and return the claim order."""
    order: list[str] = []
    while (item := user.claim_next_for_lane(conn, lane, runner_id="drain")) is not None:
        order.append(item.stable_id)
    return order


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
@pytest.mark.parametrize("lane", ["stems", "lyrics"])
def test_do_next_enqueues_selection_in_caller_order(tmp_path: Path, lane: str) -> None:
    """[if] selected ids are enqueued do-next [then] they head the lane in caller order, [else stop]."""
    conn = _conn(tmp_path)
    _enqueue(conn, lane, ["m", "a"], tmp_path, "next")
    result = _enqueue(conn, lane, ["x", "c", "q", "z", "c"], tmp_path, "next")
    assert [i.stable_id for i in result.items] == ["x", "c", "q", "z"]
    assert all(i.state == "pending" for i in result.items)
    assert _pending(conn, lane) == ["x", "c", "q", "z", "m", "a"]
    assert _drain(conn, lane) == ["x", "c", "q", "z", "m", "a"]
    conn.close()


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
@pytest.mark.parametrize("lane", ["stems", "lyrics"])
def test_tail_enqueues_selection_after_pending_in_caller_order(tmp_path: Path, lane: str) -> None:
    """[if] selected ids are enqueued at tail [then] they follow pending in caller order, [else stop]."""
    conn = _conn(tmp_path)
    _enqueue(conn, lane, ["q", "m"], tmp_path, "next")
    result = _enqueue(conn, lane, ["x", "b", "z", "a", "x"], tmp_path, "tail")
    assert [i.stable_id for i in result.items] == ["x", "b", "z", "a"]
    assert _pending(conn, lane) == ["q", "m", "x", "b", "z", "a"]
    _enqueue(conn, lane, ["c", "m"], tmp_path, "tail")
    assert _pending(conn, lane) == ["q", "x", "b", "z", "a", "c", "m"]
    assert _drain(conn, lane) == ["q", "x", "b", "z", "a", "c", "m"]
    conn.close()


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
@pytest.mark.parametrize("placement", ["next", "tail"])
def test_running_id_in_selection_is_left_running_and_order_kept(tmp_path: Path, placement: str) -> None:
    """[if] an id is already running [then] it stays running once and the rest keep caller order, [else stop]."""
    conn = _conn(tmp_path)
    _enqueue(conn, "stems", ["b", "m"], tmp_path, "next")
    running = user.claim_next_for_lane(conn, "stems", runner_id="r1")
    assert running is not None and running.stable_id == "b"
    result = _enqueue(conn, "stems", ["z", "b", "a"], tmp_path, placement)
    assert result.already_running == ("b",)
    assert [i.stable_id for i in result.items] == ["z", "b", "a"]
    still = next(i for i in result.items if i.stable_id == "b")
    assert still.state == "running"
    assert still.runner_id == "r1"
    rows = conn.execute("SELECT COUNT(*) FROM analysis_queue_item WHERE lane = 'stems' AND stable_id = 'b'").fetchone()
    assert rows[0] == 1
    expected = ["z", "a", "m"] if placement == "next" else ["m", "z", "a"]
    assert _pending(conn, "stems") == expected
    assert _drain(conn, "stems") == expected
    conn.close()
