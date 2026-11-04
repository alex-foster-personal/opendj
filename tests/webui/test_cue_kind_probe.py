"""Tests for read-only ``cue_kind_probe`` CLI (synthetic sqlite only)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from apps.adapters.rekordbox.cue_kind_probe import run_probe


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
        "Commnt VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE djmdCue ("
        "ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), "
        "InMsec INTEGER, OutMsec INTEGER, Kind INTEGER, ActiveLoop INTEGER, "
        "Comment VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
    )


def _insert_cue(
    conn: sqlite3.Connection,
    cue_id: str,
    content_id: str,
    kind: int,
    *,
    out_msec: int = 0,
    deleted: int = 0,
    comment: str = "",
) -> None:
    conn.execute(
        "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ActiveLoop, "
        "Comment, rb_local_deleted) VALUES (?, ?, 1000, ?, ?, 0, ?, ?)",
        (cue_id, content_id, out_msec, kind, comment, deleted),
    )


@pytest.fixture
def probe_db(tmp_path: Path) -> Path:
    # Synthetic fixture — not a live-library capture (issue #4069).
    db = tmp_path / "synthetic.plain.db"
    conn = sqlite3.connect(str(db))
    try:
        _schema(conn)
        conn.execute("INSERT INTO djmdContent (ID, Commnt) VALUES ('T1', 'track-one')")
        conn.execute("INSERT INTO djmdContent (ID, Commnt) VALUES ('T2', 'track-two')")
        conn.execute("INSERT INTO djmdContent (ID, Commnt) VALUES ('T3', 'track-three')")
        conn.execute("INSERT INTO djmdContent (ID, Commnt) VALUES ('T4', 'track-four')")
        for k in (1, 2, 3, 5):
            _insert_cue(conn, f"t1-{k}", "T1", k)
        _insert_cue(conn, "t1-9", "T1", 9)
        _insert_cue(conn, "t1-del", "T1", 2, deleted=1)
        for k in (1, 2, 3, 4):
            out = 5000 if k == 4 else 0
            _insert_cue(conn, f"t2-{k}", "T2", k, out_msec=out)
        _insert_cue(conn, "t3-1", "T3", 1)
        _insert_cue(conn, "t4-4", "T4", 4)
        _insert_cue(conn, "t4-9", "T4", 9)
        conn.commit()
    finally:
        conn.close()
    return db


def test_library_summary_counts(probe_db: Path) -> None:
    summary = run_probe(probe_db)
    assert summary["tracks_with_ge4_kinds_contiguous_skipping_4"] == 1
    assert summary["tracks_with_ge4_kinds_contiguous_including_4"] == 1
    assert summary["tracks_with_kind_4_and_kind_9"] == 1
    assert summary["kind_4_loop_count"] == 1
    assert "1" in summary["live_rows_per_kind"]


def test_content_id_filter_no_d_or_h_on_t3(probe_db: Path) -> None:
    payload = run_probe(probe_db, content_id="T3")
    rows = payload["tracks"][0]["rows"]
    kinds = {r["kind"] for r in rows}
    assert 1 in kinds
    slots = {r["current_app_slot"] for r in rows}
    assert "D" not in slots
    assert "H" not in slots


def test_json_has_comment_len_not_text(probe_db: Path) -> None:
    conn = sqlite3.connect(str(probe_db))
    try:
        _insert_cue(conn, "t3-secret", "T3", 1, comment="secret-text")
        conn.commit()
    finally:
        conn.close()
    payload = run_probe(probe_db, content_id="T3")
    blob = json.dumps(payload)
    assert "secret-text" not in blob
    assert any(r.get("comment_len", 0) > 0 for t in payload["tracks"] for r in t["rows"])


def test_deleted_rows_excluded(probe_db: Path) -> None:
    summary = run_probe(probe_db)
    # T1 has a deleted Kind 2 row; only T1+T2 live Kind 2 rows remain.
    assert summary["live_rows_per_kind"].get("2") == 2


def test_missing_db_exit_2() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.adapters.rekordbox.cue_kind_probe", "--db", "/no/such.db"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_probe_does_not_lock_db_for_writers(probe_db: Path) -> None:
    run_probe(probe_db)
    conn = sqlite3.connect(str(probe_db))
    try:
        conn.execute(
            "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ActiveLoop, "
            "Comment, rb_local_deleted) VALUES ('x', 'T3', 1, 0, 1, 0, '', 0)"
        )
        conn.commit()
    finally:
        conn.close()
