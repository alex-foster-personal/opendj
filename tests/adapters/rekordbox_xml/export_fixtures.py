"""State-DB fixtures for the whole-collection rekordbox XML export tests (LIBORG-10051)."""

from __future__ import annotations

import json
from pathlib import Path

from apps.analysis import store as analysis_store
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema

NOW = "2026-11-09T00:00:00Z"
MUSIC = "/Volumes/Invented DJ/Music"


def make_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    conn.close()
    return path


def add_tracks(db: Path, count: int, *, prefix: str = "t", with_fields: bool = True) -> list[str]:
    ids = [f"{prefix}{i:05d}" for i in range(count)]
    conn = state_db.open_rw(db, apply_schema=False)
    with conn:
        conn.executemany(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, album, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sid,
                    f"Song {sid} & co",
                    json.dumps([f"Artist {sid}"]),
                    "Album <1>",
                    200_000 + i,
                    f"{MUSIC}/{sid} #1 %20.mp3",
                    NOW,
                    NOW,
                )
                for i, sid in enumerate(ids)
            ],
        )
        if with_fields:
            rows = []
            for i, sid in enumerate(ids):
                rows += [
                    (sid, "bpm", json.dumps(120 + i % 10 + 0.5)),
                    (sid, "key", json.dumps("8A")),
                    (sid, "rating", json.dumps(i % 6)),
                ]
            conn.executemany(
                "INSERT INTO track_fields(stable_id, field_name, value_json, source, modified_at) "
                "VALUES (?, ?, ?, 'manual', '" + NOW + "')",
                rows,
            )
    conn.close()
    return ids


def add_node(db: Path, pid: str, name: str, kind: str, parent: str | None, sort_index: int | None) -> None:
    conn = state_db.open_rw(db, apply_schema=False)
    with conn:
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, created_at, updated_at, "
            "parent_id, kind, sort_index) VALUES (?, ?, 'open-dj', ?, ?, ?, ?, ?, ?)",
            (pid, name, pid, NOW, NOW, parent, kind, sort_index),
        )
    conn.close()


def add_members(db: Path, pid: str, stable_ids: list[str]) -> None:
    conn = state_db.open_rw(db, apply_schema=False)
    with conn:
        conn.executemany(
            "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
            [(pid, sid, pos) for pos, sid in enumerate(stable_ids)],
        )
    conn.close()


def delete_tracks(db: Path, reasons: dict[str, str]) -> None:
    conn = state_db.open_rw(db, apply_schema=False)
    with conn:
        conn.executemany(
            "UPDATE tracks SET deleted_at = ?, deleted_reason = ? WHERE stable_id = ?",
            [(NOW, reason, sid) for sid, reason in reasons.items()],
        )
    conn.close()


def make_tree_library(tmp_path: Path) -> tuple[Path, list[str]]:
    """5 tracks; Crates (folder) > House (folder) > [Warmup, Peak]."""
    db = make_db(tmp_path)
    ids = add_tracks(db, 5)
    add_node(db, "f1", "Crates", "folder", None, 0)
    add_node(db, "f2", "House", "folder", "f1", 0)
    add_node(db, "p2", "Peak", "playlist", "f2", 1)
    add_node(db, "p1", "Warmup", "playlist", "f2", 0)
    add_members(db, "p1", [ids[2], ids[0], ids[1]])
    add_members(db, "p2", [ids[4], ids[3]])
    return db, ids


def add_raw_track(db: Path, sid: str, file_path: str) -> None:
    conn = state_db.open_rw(db, apply_schema=False)
    with conn:
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, '[]', ?, ?, ?)",
            (sid, sid, file_path, NOW, NOW),
        )
    conn.close()


def set_field(db: Path, sid: str, name: str, value_json: str) -> None:
    conn = state_db.open_rw(db, apply_schema=False)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO track_fields(stable_id, field_name, value_json, source, modified_at) "
            "VALUES (?, ?, ?, 'manual', ?)",
            (sid, name, value_json, NOW),
        )
    conn.close()


def add_projection(db: Path, sid: str, field: str, value: object, status: str) -> None:
    conn = analysis_store.open_conn(db)
    with conn:
        conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status, reason, "
            "confidence, backend, backend_version, updated_at) VALUES (?, ?, ?, ?, ?, 0.9, 'own_test', '1.0', ?)",
            (sid, field, value if status == "ok" else None, status, None if status == "ok" else "test", NOW),
        )
    conn.close()
