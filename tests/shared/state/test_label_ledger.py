"""Label response storage and immediate karma grant (issue #11192).

[if] a label_response is stored [then] contribution_ledger grants karma, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("PLATFORM-12033")

_TASK_ID = "task-stems-1"
_RESPONSE_ID = "resp-1"
_ANON = "anon-sess-abc"
_NOW = "2026-11-10T15:00:00+00:00"


def _open_migrated(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    state_schema.apply_migrations(conn)
    return conn


def _seed_task(conn: sqlite3.Connection, *, reward_karma: int = 12) -> None:
    conn.execute(
        """
        INSERT INTO label_task (
            id, experiment_id, hypothesis_ids, modality, payload,
            reward_karma, max_wait_s, batch_size, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _TASK_ID,
            "exp-stems-farm",
            json.dumps(["hyp-a", "hyp-b"]),
            "stems_ab_rank",
            json.dumps({"clips": ["a", "b", "c"]}),
            reward_karma,
            120,
            3,
            _NOW,
        ),
    )
    conn.commit()


def test_response_fields_store_and_grant_karma(tmp_path: Path) -> None:
    """[if] a label_response is inserted with required fields [then] karma is granted, [else stop]."""
    conn = _open_migrated(tmp_path / "state.db")
    try:
        _seed_task(conn)
        conn.execute(
            """
            INSERT INTO label_response (
                id, task_id, anon_session, ranking, strength, cost_choice,
                ms_to_complete, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _RESPONSE_ID,
                _TASK_ID,
                _ANON,
                json.dumps(["c", "a", "b"]),
                0.85,
                "worth_extra_cost",
                42000,
                _NOW,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT karma_delta, anon_session, response_id FROM contribution_ledger").fetchone()
        assert row == (12, _ANON, _RESPONSE_ID)
    finally:
        conn.close()


def test_duplicate_task_and_anon_session_is_rejected(tmp_path: Path) -> None:
    """[if] the same response is inserted twice for the same task and anon_session [then] refuse, [else stop]."""
    conn = _open_migrated(tmp_path / "state.db")
    try:
        _seed_task(conn)
        insert = """
            INSERT INTO label_response (
                id, task_id, anon_session, ranking, strength, cost_choice,
                ms_to_complete, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        base = (
            _TASK_ID,
            _ANON,
            json.dumps(["a", "b"]),
            0.5,
            "skip",
            1000,
            _NOW,
        )
        conn.execute(insert, ("resp-first", *base))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("resp-dup", *base))
    finally:
        conn.close()


def test_user_id_may_be_omitted(tmp_path: Path) -> None:
    """[if] user_id is omitted [then] response and ledger rows store NULL user_id, [else stop]."""
    conn = _open_migrated(tmp_path / "state.db")
    try:
        _seed_task(conn, reward_karma=3)
        conn.execute(
            """
            INSERT INTO label_response (
                id, task_id, user_id, anon_session, ranking, strength, cost_choice,
                ms_to_complete, created_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                "resp-anon",
                _TASK_ID,
                _ANON,
                json.dumps(["b", "a"]),
                0.4,
                "cheap",
                800,
                _NOW,
            ),
        )
        conn.commit()
        resp = conn.execute("SELECT user_id FROM label_response WHERE id = 'resp-anon'").fetchone()
        ledger = conn.execute("SELECT user_id, karma_delta FROM contribution_ledger").fetchone()
        assert resp[0] is None
        assert ledger == (None, 3)
    finally:
        conn.close()
