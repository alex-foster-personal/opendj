"""Safety regressions for immutable, native-ID playlist writeback plans."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apps.webui.server.playlist_writeback import (
    VendorPlaylist, WritebackBackup, WritebackConflict, WritebackService,
)


def _revision(target_id: str, members: list[str]) -> str:
    return hashlib.sha256(json.dumps({"target_id": target_id, "members": members}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _FakeVendorWriter:
    vendor: str
    state_conn: sqlite3.Connection
    playlists: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    reversals: dict[str, tuple[Path, str, list[str], str]] = field(default_factory=dict)
    target_path: Path = Path("/fixture/live.db")
    calls: list[tuple] = field(default_factory=list)
    before_boundary_check: object | None = None

    def list_playlists(self) -> list[VendorPlaylist]:
        return [VendorPlaylist(playlist_id=playlist_id, name=name) for playlist_id, (name, _members) in self.playlists.items()]
    def read_members_by_id(self, playlist_id: str) -> list[str]:
        return list(self.playlists[playlist_id][1])
    def apply_with_backup_by_id(self, playlist_id: str, desired_members: list[str], stable_members: list[str], expected_target_revision: str, expected_mapping_revision: str, assert_source_current) -> tuple[WritebackBackup, str]:
        if _revision(playlist_id, self.read_members_by_id(playlist_id)) != expected_target_revision:
            raise WritebackConflict("target revision changed before transaction")
        if self.before_boundary_check is not None:
            self.before_boundary_check()
        assert_source_current()
        rows = self.state_conn.execute(
            "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? AND stable_id IN ("
            + ",".join("?" * len(set(stable_members))) + ")",
            (self.vendor, *dict.fromkeys(stable_members)),
        ).fetchall() if stable_members else []
        mapping_revision = hashlib.sha256(json.dumps(sorted((str(stable_id), str(vendor_id)) for stable_id, vendor_id in rows), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if mapping_revision != expected_mapping_revision:
            raise WritebackConflict("mapping changed inside vendor transaction")
        backup_id = f"backup-{len(self.reversals) + 1}"
        preimage = self.read_members_by_id(playlist_id)
        backup = WritebackBackup(backup_id)
        self.calls.append(("backup", self.target_path))
        self.calls.append(("replace", playlist_id, list(stable_members)))
        name, members = self.playlists[playlist_id]
        self.playlists[playlist_id] = (name, stable_members)
        post_revision = _revision(playlist_id, self.read_members_by_id(playlist_id))
        self.reversals[backup_id] = (self.target_path, playlist_id, preimage, post_revision)
        return backup, post_revision
    def restore_backup(self, backup_id: str, target_id: str, expected_target_revision: str) -> str:
        if _revision(target_id, self.read_members_by_id(target_id)) != expected_target_revision:
            raise WritebackConflict("target revision conflict")
        target_path, recorded_id, preimage, recorded_revision = self.reversals[backup_id]
        if target_path != self.target_path or recorded_id != target_id or recorded_revision != expected_target_revision:
            raise RuntimeError("reversal metadata does not match target")
        name, _members = self.playlists[target_id]
        self.playlists[target_id] = (name, list(preimage))
        return _revision(target_id, self.read_members_by_id(target_id))


@pytest.fixture
def state_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE track_vendor_ids (stable_id TEXT, vendor TEXT, vendor_id TEXT)")
    conn.executemany("INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)", [(item, f"rb-{item}") for item in ("a", "b", "c", "d")])
    yield conn
    conn.close()


@pytest.fixture
def writer(state_conn: sqlite3.Connection) -> _FakeVendorWriter:
    return _FakeVendorWriter("rekordbox", state_conn, {"one": ("Set", ["a", "c"]), "two": ("Set", ["b"])})


@pytest.fixture
def service(writer: _FakeVendorWriter) -> WritebackService:
    def factory(vendor, mode, target_path):
        assert vendor == "rekordbox" and mode == "live" and target_path == writer.target_path
        return writer
    return WritebackService(writer_factory=factory)


def _plan(service: WritebackService) -> object:
    return service.plan(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one")


def test_duplicate_names_require_and_apply_the_selected_native_id(service, writer) -> None:
    plan = _plan(service)
    assert plan.target_id == "one" and plan.target_name == "Set"
    result = service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "b"]
    assert writer.playlists["two"][1] == ["b"]
    assert result.backup_id == "backup-1"


def test_apply_rebuilds_the_selected_target_in_exact_source_order(service, writer) -> None:
    writer.playlists["one"] = ("Set", ["a", "b", "c"])
    plan = service.plan(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "c", "d"], target_mode="live", target_path="/fixture/live.db", target_id="one")
    service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "c", "d"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c", "d"]


def test_plan_token_rejects_concurrent_target_membership_before_backup(service, writer) -> None:
    plan = _plan(service)
    writer.playlists["one"] = ("Set", ["a", "c", "d"])
    with pytest.raises(WritebackConflict, match="stale"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c", "d"]
    assert not writer.reversals


def test_plan_token_rejects_concurrent_source_membership(service) -> None:
    plan = _plan(service)
    with pytest.raises(WritebackConflict, match="stale"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "c"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)


def test_vendor_boundary_rejects_source_edit_interleaved_after_plan(writer) -> None:
    source = ["a", "b"]
    service = WritebackService(
        writer_factory=lambda *_args: writer,
        source_members_reader=lambda _playlist_id: list(source),
    )
    plan = _plan(service)
    writer.before_boundary_check = lambda: source.__setitem__(slice(None), ["a", "c"])
    with pytest.raises(WritebackConflict, match="source changed"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c"]
    assert not writer.reversals


def test_vendor_boundary_rejects_mapping_remap_after_native_payload_capture(service, writer) -> None:
    plan = _plan(service)
    writer.before_boundary_check = lambda: writer.state_conn.execute(
        "UPDATE track_vendor_ids SET vendor_id = 'rb-remapped-a' WHERE vendor = 'rekordbox' AND stable_id = 'a'"
    )
    with pytest.raises(WritebackConflict, match="mapping changed"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c"]
    assert not writer.reversals


def test_dry_run_is_non_mutating_and_confirmation_is_required(service, writer) -> None:
    plan = _plan(service)
    result = service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token)
    assert result.dry_run and not writer.calls
    with pytest.raises(WritebackConflict, match="confirmed"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False)


def test_backup_is_taken_from_the_exact_target_and_rollback_is_cas_protected(service, writer) -> None:
    plan = _plan(service)
    applied = service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target_mode="live", target_path="/fixture/live.db", target_id="one", plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert ("backup", Path("/fixture/live.db")) in writer.calls
    writer.playlists["two"] = ("Set", ["b", "d"])
    reverted = service.rollback(vendor="rekordbox", target_mode="live", target_path="/fixture/live.db", target_id="one", backup_id=applied.backup_id or "", expected_target_revision=applied.target_revision or "", confirmed=True)
    assert reverted.rolled_back and writer.playlists["one"][1] == ["a", "c"]
    assert writer.playlists["two"][1] == ["b", "d"]
    with pytest.raises(WritebackConflict):
        service.rollback(vendor="rekordbox", target_mode="live", target_path="/fixture/live.db", target_id="one", backup_id=applied.backup_id or "", expected_target_revision=applied.target_revision or "", confirmed=True)


def test_wrong_target_path_is_refused_by_production_factory(monkeypatch, tmp_path) -> None:
    from apps.webui.server import playlist_writeback as module
    monkeypatch.setattr(module.paths, "REKORDBOX_LIVE_DB", tmp_path / "live.db")
    with pytest.raises(Exception):
        module.default_writer_factory("rekordbox", "live", tmp_path / "working.db")
