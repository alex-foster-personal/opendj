"""CLI tests for playlist sets (SET-05).

[if] the CLI performs a set [then] play_count increments and membership stays put, [else stop].
[if] the playlist-sets CLI diverges from the store contract [then] fail, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from apps.shared.state import db as state_db

pytestmark = pytest.mark.requirement("SET-05")


def _seed_db(path: Path) -> None:
    conn = state_db.open_rw(path)
    apply_play_order_migrations(conn)
    apply_playlist_set_migrations(conn)
    conn.close()


def _run_cli(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.playlist_sets", "--db", str(db), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_create_then_list(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_db(db)
    assert _run_cli(db, "create", "--playlist", "pl1", "--name", "A").returncode == 0
    assert _run_cli(db, "create", "--playlist", "pl1", "--name", "B").returncode == 0
    listed = _run_cli(db, "list", "--playlist", "pl1")
    assert listed.returncode == 0
    names = {line.split("\t", 1)[0] for line in listed.stdout.strip().splitlines()}
    assert names == {"A", "B"}


def test_perform_increments_membership_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_db(db)
    created = _run_cli(db, "--json", "create", "--playlist", "pl1", "--name", "Gig")
    set_id = json.loads(created.stdout)["id"]
    conn = sqlite3.connect(db)
    before_entries = conn.execute(
        "SELECT stable_id, position FROM playlist_set_entries WHERE set_id=?",
        (set_id,),
    ).fetchall()
    performed = _run_cli(db, "--json", "perform", "--set", str(set_id))
    assert performed.returncode == 0
    body = json.loads(performed.stdout)
    assert body["kind"] == "performance"
    assert body["play_count"] == 1
    after_entries = conn.execute(
        "SELECT stable_id, position FROM playlist_set_entries WHERE set_id=?",
        (set_id,),
    ).fetchall()
    conn.close()
    assert before_entries == after_entries


def test_practice_does_not_increment(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_db(db)
    created = _run_cli(db, "--json", "create", "--playlist", "pl1", "--name", "Rehearsal")
    set_id = json.loads(created.stdout)["id"]
    practiced = _run_cli(db, "--json", "practice", "--set", str(set_id))
    assert practiced.returncode == 0
    body = json.loads(practiced.stdout)
    assert body["kind"] == "practice"
    assert body["play_count"] == 0
