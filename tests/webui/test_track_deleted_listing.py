"""LIBM-140 agent parity: removed tracks can be seen and reversed over HTTP and CLI.

[if] a track is removed [then] GET /tracks/deleted and the ``deleted`` CLI verb
both name it, and the ``undelete`` verb restores it, [else stop].

[if] nothing is removed [then] both listings are empty, not an error, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import cli as state_cli
from tests.webui.test_track_remove_undelete import (  # noqa: F401  (fixtures)
    TRACK_OTHER,
    TRACK_T,
    client,
    db_path,
)

pytestmark = pytest.mark.requirement("LIBM-140")


def _cli(state_db: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = state_cli.main(["--db", str(state_db), *argv])
    return code, capsys.readouterr().out


def test_deleted_route_lists_only_removed_tracks(client: TestClient) -> None:  # noqa: F811
    empty = client.get("/api/v1/tracks/deleted")
    assert empty.status_code == 200, empty.text
    assert empty.json() == []

    removed = client.post(f"/api/v1/tracks/{TRACK_T}:remove")
    assert removed.status_code == 200, removed.text

    listed = client.get("/api/v1/tracks/deleted")
    assert listed.status_code == 200, listed.text
    assert [(row["stable_id"], row["title"], row["deleted_at"], row["reason"]) for row in listed.json()] == [
        (TRACK_T, "Target", removed.json()["deleted_at"], "user")
    ]

    restored = client.post(f"/api/v1/tracks/{TRACK_T}:undelete")
    assert restored.status_code == 200, restored.text
    assert client.get("/api/v1/tracks/deleted").json() == []


def test_cli_lists_and_restores_a_removed_track(
    client: TestClient,  # noqa: F811
    db_path: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _cli(db_path, capsys, "deleted")
    assert (code, json.loads(out)) == (0, [])
    assert client.post(f"/api/v1/tracks/{TRACK_T}:remove").status_code == 200

    code, out = _cli(db_path, capsys, "deleted")
    assert code == 0
    assert [row["stable_id"] for row in json.loads(out)] == [TRACK_T]

    code, out = _cli(db_path, capsys, "undelete", TRACK_T)
    assert code == 0
    assert json.loads(out)["deleted_at"] is None
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT deleted_at, restored_at FROM tracks WHERE stable_id = ?", (TRACK_T,)).fetchone()
    finally:
        conn.close()
    assert row[0] is None, "the CLI restore did not persist"
    assert row[1] is not None

    # Controls: a live track and an unknown id are refused, loudly.
    assert state_cli.main(["--db", str(db_path), "undelete", TRACK_OTHER]) == 1
    assert state_cli.main(["--db", str(db_path), "undelete", "no-such-track"]) == 1
