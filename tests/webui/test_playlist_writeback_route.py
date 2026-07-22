"""HTTP parity tests for ID-targeted plan, apply, and rollback."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.playlist_writeback import VendorPlaylist, WritebackBackup, WritebackConflict, WritebackService
from apps.webui.server.routes.playlist_writeback import get_writeback_service


class _Writer:
    vendor = "rekordbox"
    def __init__(self) -> None:
        self.state_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.state_conn.execute("CREATE TABLE track_vendor_ids (stable_id TEXT, vendor TEXT, vendor_id TEXT)")
        self.state_conn.executemany("INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)", [(f"track-{i:03d}", f"rb-{i}") for i in range(1, 6)])
        self.members = ["track-001"]
        self.backup = list(self.members)
    def list_playlists(self):
        return [VendorPlaylist("native-1", "Opener Set")]

    def read_members_by_id(self, playlist_id):
        assert playlist_id == "native-1"
        return list(self.members)
    def apply_with_backup_by_id(self, playlist_id, desired_members, _stable_members, expected, _mapping_revision):
        current = self.read_members_by_id(playlist_id)
        revision = hashlib.sha256(json.dumps({"target_id": playlist_id, "members": current}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if revision != expected:
            raise WritebackConflict("stale")
        self.backup = current
        self.members = [self.state_conn.execute("SELECT stable_id FROM track_vendor_ids WHERE vendor = ? AND vendor_id = ?", (self.vendor, member)).fetchone()[0] for member in desired_members]
        after = hashlib.sha256(json.dumps({"target_id": playlist_id, "members": self.members}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return WritebackBackup("b1"), after
    def restore_backup(self, backup_id, target_id, expected):
        current = hashlib.sha256(json.dumps({"target_id": target_id, "members": self.members}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if current != expected:
            raise WritebackConflict("conflict")
        self.members = list(self.backup)
        return hashlib.sha256(json.dumps({"target_id": target_id, "members": self.members}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def client(seed_backend: InMemoryBackend) -> Iterator[TestClient]:
    writer = _Writer()
    app = create_app(backend=seed_backend, bind_host="127.0.0.1", hostname="test-host", lock_status_fn=lambda: None)
    app.dependency_overrides[get_writeback_service] = lambda: WritebackService(writer_factory=lambda *_args: writer)
    with TestClient(app) as result:
        yield result


def _query() -> str: return "vendor=rekordbox&target_mode=live&target_path=%2Ffixture%2Flive.db&target_id=native-1"


def test_http_plan_apply_and_rollback_share_serializable_contract(client: TestClient) -> None:
    targets = client.get("/api/v1/playlists/pl-001/writeback/targets?" + _query().replace("&target_id=native-1", ""))
    assert targets.status_code == 200 and targets.json()["targets"] == [{"playlist_id": "native-1", "name": "Opener Set"}]
    plan = client.get("/api/v1/playlists/pl-001/writeback/plan?" + _query())
    assert plan.status_code == 200
    token = plan.json()["plan_token"]
    refused = client.post("/api/v1/playlists/pl-001/writeback/apply", json={"vendor":"rekordbox","target_mode":"live","target_path":"/fixture/live.db","target_id":"native-1","plan_token":token,"dry_run":False})
    assert refused.status_code == 409
    applied = client.post("/api/v1/playlists/pl-001/writeback/apply", json={"vendor":"rekordbox","target_mode":"live","target_path":"/fixture/live.db","target_id":"native-1","plan_token":token,"dry_run":False,"confirmed":True})
    assert applied.status_code == 200 and applied.json()["backup_id"] == "b1"
    body = applied.json()
    rolled_back = client.post("/api/v1/playlists/pl-001/writeback/rollback", json={"vendor":"rekordbox","target_mode":"live","target_path":"/fixture/live.db","target_id":"native-1","backup_id":"b1","expected_target_revision":body["target_revision"],"confirmed":True})
    assert rolled_back.status_code == 200 and rolled_back.json()["rolled_back"] is True


def test_http_plan_refuses_missing_vendor_id(client: TestClient) -> None:
    response = client.get("/api/v1/playlists/pl-001/writeback/plan?" + _query().replace("native-1", "wrong"))
    assert response.status_code == 409
