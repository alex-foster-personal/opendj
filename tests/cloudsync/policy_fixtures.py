"""A small real fleet for the policy CLI and HTTP tests.

Everything is written through the production paths: the state DB is migrated
by ``state_db.open_rw``, this machine registers through
``sync_stamp.ensure_local_machine``, and policy cells go through
``policy_store.apply_proposal``. Only library rows (tracks, playlists,
locations) and a remote ``machines`` row, which a real fleet would receive by
sync, are inserted directly.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.state.migrations_v10 import ASSET_KIND_CHECK_VALUES
from apps.sync_hub import client as sync_client
from apps.sync_hub import policy_store
from apps.sync_hub.policy_rules import PolicyCell
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

HUB_ID: str = "policy-test-hub"
OTHER_ID: str = "policy-test-other"
PLAYLIST_ID: str = "pl-gig"
TRACKS: tuple[str, ...] = ("pt-track-1", "pt-track-2")
STAMP: str = "2026-09-11T12:00:00.000000+00:00"


def add_remote_machine(conn: sqlite3.Connection, machine_id: str, *, is_hub: int) -> None:
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, last_seen) "
        "VALUES (?, ?, 'linux', ?, ?, ?)",
        (machine_id, machine_id, is_hub, STAMP, STAMP),
    )


def seed_crate(conn: sqlite3.Connection) -> None:
    """Two tracks in one live playlist. No locations: callers add those per machine."""
    for stable_id in TRACKS:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "duration_ms, created_at, updated_at) VALUES (?, 'inferred', ?, ?, 300000, ?, ?)",
            (stable_id, stable_id, json.dumps(["Test Artist"]), STAMP, STAMP),
        )
    conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, "
        "updated_at) VALUES (?, 'Gig Crate', 'rekordbox', ?, ?, ?)",
        (PLAYLIST_ID, PLAYLIST_ID, STAMP, STAMP),
    )
    for position, stable_id in enumerate(TRACKS):
        conn.execute(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES (?, ?, ?)",
            (PLAYLIST_ID, stable_id, position),
        )


def add_local_copy(conn: sqlite3.Connection, stable_id: str, machine_id: str) -> None:
    conn.execute(
        "INSERT INTO track_locations (stable_id, machine_id, kind, file_path, available, "
        "created_at, updated_at) VALUES (?, ?, 'local', ?, 1, ?, ?)",
        (stable_id, machine_id, f"/music/{stable_id}.flac", STAMP, STAMP),
    )


def make_fleet_dir(root: Path, *, with_hub: bool) -> Path:
    """A data dir holding a migrated state DB, this machine registered (no cells),
    the crate, and, when ``with_hub``, a hub pinning every asset kind."""
    data_dir = root / "data"
    conn = state_db.open_rw(sync_client.state_db_path(data_dir))
    try:
        local_id = sync_stamp.ensure_local_machine(conn)
        seed_crate(conn)
        if with_hub:
            add_remote_machine(conn, HUB_ID, is_hub=1)
            cells = tuple(PolicyCell(HUB_ID, k, "pinned", None) for k in ASSET_KIND_CHECK_VALUES)
            seeded = policy_store.apply_proposal(
                conn, replace(policy_store.empty_proposal(local_id), policies=cells), live=True
            )
            assert seeded.written, seeded.to_wire()
    finally:
        conn.close()
    return data_dir


def local_id(data_dir: Path) -> str:
    conn = sqlite3.connect(sync_client.state_db_path(data_dir))
    try:
        return sync_stamp.local_machine_id(conn)
    finally:
        conn.close()


def count(data_dir: Path, sql: str) -> int:
    conn = sqlite3.connect(sync_client.state_db_path(data_dir))
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def http_client(data_dir: Path) -> TestClient:
    """The full webui app over ``data_dir``: the policy routes arrive through
    ``app_wiring``, not through a test-side ``include_router``."""
    db_path = sync_client.state_db_path(data_dir)
    return TestClient(
        create_app(
            backend=SqliteBackend(db_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(db_path),
            mount_frontend=False,
        )
    )
