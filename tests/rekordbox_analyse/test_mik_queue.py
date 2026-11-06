"""RBXAN-8322h: the MIK queue scopes Open DJ tracks by playlist prefix and skips tombstones.

[if] the MIK queue is built for a playlist prefix [then] it lists only live tracks in matching playlists, [else stop].

-Claude
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.mik import queue
from apps.shared.state import db as state_db

pytestmark = pytest.mark.requirement("RBXAN-8322h")


def test_scope_by_playlist_prefix_and_skip_deleted(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = state_db.open_rw(db)
    now = "2026-11-05T00:00:00+00:00"
    with conn:
        for sid, path, deleted in [("a", "/m/a.mp3", None), ("b", "/m/b.mp3", None), ("c", "/m/c.mp3", now)]:
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, file_path, created_at, updated_at, deleted_at) "
                "VALUES (?, 'inferred', ?, ?, ?, ?)",
                (sid, path, now, now, deleted),
            )
        for pid, name in [("p1", "Viva - The cut"), ("p2", "House")]:
            conn.execute(
                "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
                "VALUES (?, ?, 'odj', ?, ?, ?)",
                (pid, name, pid, now, now),
            )
        for pid, sid, pos in [("p1", "a", 0), ("p2", "b", 0), ("p1", "c", 1)]:
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES (?, ?, ?)", (pid, sid, pos)
            )
    conn.close()
    assert queue._open_dj_paths(db, ("Viva",)) == {"/m/a.mp3"}, (
        "if out-of-scope or deleted tracks are sent to MIK then broken"
    )
    assert queue._open_dj_paths(db, ()) == {"/m/a.mp3", "/m/b.mp3"}


def test_send_requires_accepting_mik_tag_writes(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        queue.main(["--state-db", str(tmp_path / "x.db"), "--send"])
