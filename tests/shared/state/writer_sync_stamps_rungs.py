"""Writer exercises for the synced tables added by schema rungs v10 and v11.

Moved out of ``test_writer_sync_stamps.py`` when v11 pushed that file past the
quality gate's 600-line ``file_size`` ratchet, the same move round 4 made for
``test_writer_sync_stamps_locations.py``. Nothing was cut: both exercises are
the originals, and the tripwire still calls them from
``_exercise_every_writer_path``.

This is a helper module, not a test module (no ``test_`` prefix), so pytest
does not collect it. It takes the stable id and timestamp from the caller
rather than importing them back, because the tripwire module imports this one.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from apps.shared.state import sync_stamp
from apps.webui.server.routes import feedback_replica


def exercise_lyric_verdict_writes(
    conn: sqlite3.Connection, machine_id: str, sid: str, ts: str
) -> None:
    """Drive the ``lyric_verdict`` write shape (schema v10, D13.1).

    Written against ``stamped_transaction`` + ``stamp_and_log`` directly
    rather than through ``apps.lyrics.store``, for the same reason
    ``_exercise_hydration_writers`` calls two module-private writers: it
    is the CHOKEPOINT that is under test here, and reaching it through the
    lyrics store would drag that package's validation and its verdict
    vocabulary into a test about stamping. What this pins is that a v10 row
    can only be written the stamped way -- an insert AND an update, because
    the ON CONFLICT branch is where the round 2 finding N1 writers all
    slipped through.

    ``open_rw`` connections are AUTOCOMMIT, so each write owns its own
    ``stamped_transaction`` and there is no trailing ``conn.commit()``.
    """
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(conn, "lyric_verdict", (sid,), machine_id)
        conn.execute(
            "INSERT INTO lyric_verdict(stable_id, verdict, coverage_pct, "
            "source, n_words, n_lines, pct_witness_red, pipeline_version, "
            "words_content_hash, computed_at, updated_at, origin_device_id) "
            "VALUES (?, 'vocal', 91.5, 'lrclib', 240, 41, 0.012, "
            "'2026.09.09-round3a', ?, ?, ?, ?)",
            (sid, "c" * 64, ts, stamp.updated_at, stamp.origin_device_id),
        )
    # The recompute branch: same row, new numbers, second stamp.
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(conn, "lyric_verdict", (sid,), machine_id)
        conn.execute(
            "UPDATE lyric_verdict SET verdict = 'sparse', coverage_pct = 12.0, "
            "updated_at = ?, origin_device_id = ? WHERE stable_id = ?",
            (stamp.updated_at, stamp.origin_device_id, sid),
        )


def exercise_feedback_pin_reconcile(
    conn: sqlite3.Connection, tmp_path: Path, ts: str
) -> None:
    """Drive the ONLY ``feedback_pins`` writer (schema v11, FBSYNC-01).

    Through the real bridge, not a hand-rolled INSERT: the reconcile is the
    writer, and what this pins is that a pin reaching the table from a
    ``comments.json`` is always stamped and logged -- on first export AND on
    the re-export of an edit, the ON CONFLICT branch.
    """
    root = tmp_path / "feedback"
    root.mkdir(parents=True, exist_ok=True)
    pin = {
        "id": "tripwirepin1", "x_pct": 10.0, "y_pct": 20.0, "anchor": None,
        "page": "/performance", "text": "tripwire", "created_at": ts,
        "build": {"git_sha": "deadbeef", "built_at_utc": ts, "source": "repo"},
    }
    (root / "comments.json").write_text(json.dumps({"comments": [pin]}), encoding="utf-8")
    feedback_replica.reconcile(conn, root)
    edited = {**pin, "status": "fixed", "updated_at": "2026-08-30T10:00:00+00:00"}
    (root / "comments.json").write_text(json.dumps({"comments": [edited]}), encoding="utf-8")
    feedback_replica.reconcile(conn, root)
