"""Cloud offload over HTTP: the routes the menu, CLI and MCP tools all call.

The real webui app over a real state.db, driven through ``TestClient``. The
two object stores are the in-memory doubles from ``conftest`` (the live R2
test checks them against R2); R2 credentials are dummy env values so
``CloudConfig.from_env`` runs its real path.

* [if] a track is offloaded over HTTP [then] its files are gone, the operation
  reports bytes and counts, and the library listing still has the row, marked
  ``offload.state == "offloaded"`` (VETO: never hidden) [else stop];
* [if] the deck asks for an offloaded track's audio [then] it gets 409
  ``TRACK_OFFLOADED``, not a missing-file 404;
* [if] R2 credentials are absent [then] 503 names them and nothing moves.

- [if] a track is offloaded over HTTP [then] its row stays listed and marked offloaded, [else stop].
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.cloud.lock import FakeS3Client
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

from .conftest import InMemoryAssetS3

pytestmark = pytest.mark.requirement("OFFLOAD-8598d")

SID = "d" * 40
BODY = b"real-audio-bytes-for-the-route-test" * 50
R2_ENV = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKID", "R2_SECRET_ACCESS_KEY": "secret"}


@pytest.fixture
def stores() -> tuple[InMemoryAssetS3, FakeS3Client]:
    return InMemoryAssetS3(), FakeS3Client()


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "music" / "song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(BODY)
    return path


@pytest.fixture
def http(tmp_path: Path, audio: Path, stores, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    db_path = tmp_path / "data" / "state" / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    sync_stamp.ensure_local_machine(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, 'inferred', 'route song', ?, ?, ?)",
        (SID, str(audio), sync_stamp.canonical_now(), sync_stamp.canonical_now()),
    )
    conn.commit()
    conn.close()
    # The rekordbox adapter (the deck's first candidate path, and the audio
    # route) reads its process-global library paths; point them at this one.
    from apps.adapters.rekordbox import config as rb_config

    monkeypatch.setattr(rb_config, "STATE_DB", db_path)
    monkeypatch.setattr(rb_config, "DATA_DIR", db_path.parent.parent)
    app = create_app(
        backend=SqliteBackend(db_path),
        hostname="offload-test",
        state_db_path=str(db_path),
        mount_frontend=False,
        port=8686,
        frontend_port=5173,
    )
    app.state.cloud_offload_clients = lambda _cfg: stores
    app.state.stems_dir = tmp_path / "stems"
    with TestClient(app, base_url="http://127.0.0.1:8686") as client:
        yield client


def _wait(http: TestClient, op: dict) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = http.get(f"/api/v1/offload/operations/{op['op_id']}").json()
        if current["state"] not in ("queued", "running"):
            return current
        time.sleep(0.05)
    raise AssertionError(f"operation {op['op_id']} never finished: {current}")


def test_offload_then_restore_round_trip_over_http(http, audio, stores):
    started = http.post("/api/v1/offload/tracks", json={"stable_ids": [SID]})
    assert started.status_code == 202, started.text
    done = _wait(http, started.json())
    assert done["state"] == "done", done
    assert done["results"][SID]["state"] == "offloaded"
    assert done["bytes_total"] == len(BODY) and done["bytes_done"] == len(BODY)
    assert done["bytes_freed"] == len(BODY) and done["tracks_done"] == 1
    assert not audio.exists()

    states = http.get("/api/v1/offload/tracks", params={"stable_ids": SID}).json()
    assert states[SID]["state"] == "offloaded"
    assert http.get("/api/v1/offload/summary").json()["bytes_freed"] == len(BODY)

    restored = _wait(http, http.post("/api/v1/offload/restore", json={"stable_ids": [SID], "mode": "temporary"}).json())
    assert restored["results"][SID]["state"] == "temporary"
    assert audio.read_bytes() == BODY


def test_offloaded_row_stays_listed_and_marked(http, audio):
    """VETO: an offloaded track is never dropped from a list."""
    _wait(http, http.post("/api/v1/offload/tracks", json={"stable_ids": [SID]}).json())
    listing = http.get("/api/v1/tracks", params={"limit": 50})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    rows = body["items"]
    row = next(r for r in rows if r["stable_id"] == SID)
    assert row["offload"]["state"] == "offloaded"
    assert row["file_exists"] is not True


def test_deck_load_of_an_offloaded_track_answers_track_offloaded(http, audio):
    _wait(http, http.post("/api/v1/offload/tracks", json={"stable_ids": [SID]}).json())
    from fastapi import HTTPException

    from apps.adapters.rekordbox.paths import resolve_playable_audio

    with pytest.raises(HTTPException) as refused:
        resolve_playable_audio(SID)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "TRACK_OFFLOADED"


def test_missing_credentials_answer_503_and_move_nothing(http, audio, monkeypatch, stores):
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY")
    answer = http.post("/api/v1/offload/tracks", json={"stable_ids": [SID]})
    assert answer.status_code == 503
    assert answer.json()["detail"]["code"] == "OFFLOAD_CREDENTIALS_MISSING"
    assert "R2_SECRET_ACCESS_KEY" in answer.json()["detail"]["message"]
    assert audio.exists() and stores[0].put_calls == []


def test_unknown_operation_and_playlist_are_404(http):
    assert http.get("/api/v1/offload/operations/nope").status_code == 404
    assert http.post("/api/v1/offload/operations/nope/cancel").status_code == 404
    answer = http.post("/api/v1/offload/playlists/no-such-playlist")
    assert answer.status_code == 404 and answer.json()["detail"]["code"] == "PLAYLIST_NOT_FOUND"


def test_sweep_dry_run_reports_without_deleting(http, audio):
    _wait(http, http.post("/api/v1/offload/tracks", json={"stable_ids": [SID]}).json())
    _wait(http, http.post("/api/v1/offload/restore", json={"stable_ids": [SID], "mode": "temporary"}).json())
    swept = _wait(http, http.post("/api/v1/offload/sweep", json={"dry_run": True}).json())
    assert swept["state"] == "done" and swept["dry_run"] is True
    assert swept["report"]["reoffloaded"] == []  # not expired yet
    assert audio.exists()


def test_operation_wire_is_a_snapshot_not_a_live_view():
    """Sentry: [if] the worker mutates an operation while it is serialized [then] the wire copy is unaffected, [else stop]."""
    from apps.webui.server.cloud_offload_service import Operation

    op = Operation("op1", "offload", ["a"])
    op.results["a"] = {"state": None, "note": None, "error": None}
    wire = op.wire()
    op.results["b"] = {"state": "offloaded", "note": None, "error": None}
    op.results["a"]["error"] = "late"
    op.stable_ids.append("b")
    assert wire["results"] == {"a": {"state": None, "note": None, "error": None}}
    assert wire["stable_ids"] == ["a"]
