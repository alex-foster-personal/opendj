"""Safety regressions for immutable, native-ID playlist writeback plans."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apps.webui.server.playlist_writeback import (
    VendorPlaylist,
    WritebackBackup,
    WritebackConflict,
    WritebackService,
    WritebackTarget,
    WritebackUnavailable,
)
from apps.webui.server.routes.playlist_writeback import get_writeback_service
from apps.webui.server.sqlite_backend import SqliteBackend

# This module exercises live-write MECHANICS against tmp fixtures, so it runs
# with the one-way rekordbox import gate ON (root conftest reads the marker).
# It never touches a real rekordbox target.
pytestmark = [pytest.mark.rekordbox_writeback, pytest.mark.rb_parity]


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
    after_mapping_check: object | None = None
    source: list[str] = field(default_factory=lambda: ["a", "b"])

    def list_playlists(self) -> list[VendorPlaylist]:
        return [VendorPlaylist(playlist_id=playlist_id, name=name) for playlist_id, (name, _members) in self.playlists.items()]
    def read_members_by_id(self, playlist_id: str) -> list[str]:
        return list(self.playlists[playlist_id][1])
    def apply_with_backup_by_id(self, playlist_id: str, stable_members: list[str], expected_target_revision: str, expected_mapping_revision: str, source_transaction) -> tuple[WritebackBackup, str]:
        with source_transaction():
            if _revision(playlist_id, self.read_members_by_id(playlist_id)) != expected_target_revision:
                raise WritebackConflict("target revision changed before transaction")
            if self.before_boundary_check is not None:
                self.before_boundary_check()
            rows = self.state_conn.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? AND stable_id IN ("
                + ",".join("?" * len(set(stable_members))) + ")",
                (self.vendor, *dict.fromkeys(stable_members)),
            ).fetchall() if stable_members else []
            mapping_revision = hashlib.sha256(json.dumps(sorted((str(stable_id), str(vendor_id)) for stable_id, vendor_id in rows), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if mapping_revision != expected_mapping_revision:
                raise WritebackConflict("mapping changed inside vendor transaction")
            native_mapping = {str(stable_id): str(vendor_id) for stable_id, vendor_id in rows}
            native_members = [native_mapping[stable_id] for stable_id in stable_members]
            if self.after_mapping_check is not None:
                self.after_mapping_check()
            backup_id = f"backup-{len(self.reversals) + 1}"
            preimage = self.read_members_by_id(playlist_id)
            backup = WritebackBackup(backup_id)
            self.calls.append(("backup", self.target_path))
            self.calls.append(("native", playlist_id, native_members))
            self.calls.append(("replace", playlist_id, list(stable_members)))
            name, _members = self.playlists[playlist_id]
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
    conn.executemany("INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)", [(item, f"rb-{item}") for item in ("a", "b", "c", "d")])
    yield conn
    conn.close()


@pytest.fixture
def writer(state_conn: sqlite3.Connection) -> _FakeVendorWriter:
    return _FakeVendorWriter("rekordbox", state_conn, {"one": ("Set", ["a", "c"]), "two": ("Set", ["b"])})


@pytest.fixture
def service(writer: _FakeVendorWriter) -> WritebackService:
    def factory(vendor, mode, target_path):
        assert vendor == "rekordbox" and mode == "live" and target_path == writer.target_path
        return nullcontext(writer)
    return WritebackService(
        writer_factory=factory,
        source_members_reader=lambda _playlist_id: list(writer.source),
        source_lock_factory=nullcontext,
    )


def _plan(service: WritebackService, writer: _FakeVendorWriter, desired_ids: list[str] | None = None) -> object:
    desired = desired_ids or ["a", "b"]
    writer.source = list(desired)
    return service.plan(
        vendor="rekordbox", source_playlist_id="source", desired_ids=desired,
        target_mode="live", target_path="/fixture/live.db", target_id="one",
    )


def test_duplicate_names_require_and_apply_the_selected_native_id(service, writer) -> None:
    plan = _plan(service, writer)
    assert plan.target_id == "one" and plan.target_name == "Set"
    result = service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "b"]
    assert writer.playlists["two"][1] == ["b"]
    assert result.backup_id == "backup-1"


def test_apply_rebuilds_the_selected_target_in_exact_source_order(service, writer) -> None:
    writer.playlists["one"] = ("Set", ["a", "b", "c"])
    plan = _plan(service, writer, ["a", "c", "d"])
    service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "c", "d"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c", "d"]


def test_plan_token_rejects_concurrent_target_membership_before_backup(service, writer) -> None:
    plan = _plan(service, writer)
    writer.playlists["one"] = ("Set", ["a", "c", "d"])
    with pytest.raises(WritebackConflict, match="stale"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c", "d"]
    assert not writer.reversals


def test_plan_token_rejects_concurrent_source_membership(service, writer) -> None:
    plan = _plan(service, writer)
    with pytest.raises(WritebackConflict, match="stale"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "c"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)


def test_vendor_boundary_rejects_source_edit_interleaved_after_plan(writer) -> None:
    source = ["a", "b"]
    service = WritebackService(
        writer_factory=lambda *_args: nullcontext(writer),
        source_members_reader=lambda _playlist_id: list(source),
        source_lock_factory=nullcontext,
    )
    plan = _plan(service, writer)
    source[:] = ["a", "c"]
    with pytest.raises(WritebackConflict, match="source changed"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c"]
    assert not writer.reversals


def test_vendor_boundary_rejects_mapping_remap_inside_vendor_transaction(service, writer) -> None:
    plan = _plan(service, writer)
    writer.before_boundary_check = lambda: writer.state_conn.execute(
        "UPDATE track_vendor_ids SET vendor_id = 'rb-remapped-a' WHERE vendor = 'rekordbox' AND stable_id = 'a'"
    )
    with pytest.raises(WritebackConflict, match="mapping changed"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert writer.playlists["one"][1] == ["a", "c"]
    assert not writer.reversals


def test_vendor_boundary_derives_native_occurrences_from_the_checked_mapping_rows(service, writer) -> None:
    plan = _plan(service, writer)
    writer.before_boundary_check = lambda: (
        writer.state_conn.execute("UPDATE track_vendor_ids SET vendor_id = 'rb-remapped-a' WHERE vendor = 'rekordbox' AND stable_id = 'a'"),
        writer.state_conn.execute("UPDATE track_vendor_ids SET vendor_id = 'rb-a' WHERE vendor = 'rekordbox' AND stable_id = 'a'"),
    )
    service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert ("native", "one", ["rb-a", "rb-b"]) in writer.calls


def test_live_apply_refuses_missing_source_ownership(service, writer) -> None:
    plan = _plan(service, writer)
    service._source_lock_factory = None
    with pytest.raises(WritebackUnavailable, match="ownership lock"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)


def test_source_lock_blocks_source_edit_and_remap_after_mapping_cas(tmp_path) -> None:
    """Both source and mapping writers lose their lock race before mutation."""
    from apps.shared.state import db as state_db

    state_path = tmp_path / "state.db"
    with state_db.open_rw(state_path) as conn:
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, created_at, updated_at) VALUES (?, 'inferred', 't', 't')",
            [("a",), ("b",), ("c",)],
        )
        conn.execute("INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) VALUES ('source', 'Source', 'webui', 'source', 't', 't')")
        conn.executemany(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES ('source', ?, ?)", [("a", 0), ("b", 1)],
        )
        conn.executemany(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)", [("a", "rb-a"), ("b", "rb-b"), ("c", "rb-c")],
        )
    state_conn = sqlite3.connect(state_path, isolation_level=None)
    writer = _FakeVendorWriter("rekordbox", state_conn, {"one": ("Set", ["a", "c"]), "two": ("Set", ["b"])})
    backend = SqliteBackend(state_path)
    service = WritebackService(
        writer_factory=lambda *_args: nullcontext(writer),
        source_members_reader=lambda playlist_id: list(backend.get_playlist(playlist_id).items),
        source_lock_factory=backend.hold_writeback_source_lock,
    )
    plan = service.plan(
        vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"],
        target_mode="live", target_path="/fixture/live.db", target_id="one",
    )
    rejected: list[str] = []

    def mutate_after_mapping_cas() -> None:
        contender = sqlite3.connect(state_path, isolation_level=None)
        try:
            contender.execute("PRAGMA busy_timeout = 0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
            rejected.extend(["source", "mapping"])
        finally:
            contender.close()

    writer.after_mapping_check = mutate_after_mapping_cas
    service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert rejected == ["source", "mapping"]
    assert ("native", "one", ["rb-a", "rb-b"]) in writer.calls
    assert backend.get_playlist("source").items == ["a", "b"]
    assert state_conn.execute("SELECT vendor_id FROM track_vendor_ids WHERE stable_id = 'a'").fetchone()[0] == "rb-a"
    state_conn.close()


def test_production_service_binds_mapping_reads_to_locked_custom_state_db(
    monkeypatch, tmp_path,
) -> None:
    """The production writer and source lock share one custom state DB."""
    from apps.shared.state import db as state_db
    from apps.shared.state import paths as state_paths
    from apps.smartlists import djay_writer as djay_writer_module
    from apps.webui.server import playlist_writeback as writeback_module

    state_path = tmp_path / "custom-state.db"
    decoy_path = tmp_path / "default-state.db"
    for path, prefix in ((state_path, "djay"), (decoy_path, "decoy")):
        with state_db.connect_rw(path) as conn:
            conn.executemany(
                "INSERT INTO tracks (stable_id, stable_id_tier, created_at, updated_at) "
                "VALUES (?, 'inferred', 't', 't')",
                [("a",), ("b",), ("c",)],
            )
            conn.executemany(
                "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'djay', ?)",
                [(stable_id, f"{prefix}-{stable_id}") for stable_id in ("a", "b", "c")],
            )
            if path == state_path:
                conn.execute("INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) VALUES ('source', 'Source', 'webui', 'source', 't', 't')")
                conn.executemany(
                    "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES ('source', ?, ?)",
                    [("a", 0), ("b", 1)],
                )

    target_path = tmp_path / "MediaLibrary.db"
    target_path.touch()
    monkeypatch.setattr(state_paths, "STATE_DB", decoy_path)
    monkeypatch.setattr(writeback_module.paths, "DJAY_LIVE_DB", target_path)

    shared_playlists = {"one": ("Set", ["a", "c"])}
    built_writers: list[_FakeVendorWriter] = []
    mapping_paths: list[Path] = []
    rejected: list[str] = []

    def reject_custom_state_remap() -> None:
        contender = sqlite3.connect(state_path, isolation_level=None)
        try:
            contender.execute("PRAGMA busy_timeout = 0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
            rejected.append("custom-state")
        finally:
            contender.close()

    def build_writer(
        djay_db_path: Path | None = None,
        state_conn: sqlite3.Connection | None = None,
    ) -> _FakeVendorWriter:
        mapping_conn = state_conn if state_conn is not None else state_db.open_ro()
        mapping_path = mapping_conn.execute("PRAGMA database_list").fetchone()[2]
        mapping_paths.append(Path(mapping_path).resolve())
        writer = _FakeVendorWriter(
            "djay", mapping_conn, shared_playlists, target_path=Path(djay_db_path or target_path),
            after_mapping_check=reject_custom_state_remap,
        )
        built_writers.append(writer)
        return writer

    monkeypatch.setattr(djay_writer_module, "build_djay_writer", build_writer)
    backend = SqliteBackend(state_path)
    service = get_writeback_service(backend)
    plan = service.plan(
        vendor="djay", source_playlist_id="source", desired_ids=["a", "b"],
        target_mode="live", target_path=str(target_path), target_id="one",
    )
    service.apply(
        vendor="djay", source_playlist_id="source", desired_ids=["a", "b"],
        target=WritebackTarget("live", str(target_path), "one"),
        plan_token=plan.plan_token, dry_run=False, confirmed=True,
    )

    assert set(mapping_paths) == {state_path.resolve()}
    assert rejected == ["custom-state"]
    assert any(("native", "one", ["djay-a", "djay-b"]) in writer.calls for writer in built_writers)
    for built_writer in built_writers:
        _assert_connection_closed(built_writer.state_conn)


def test_dry_run_is_non_mutating_and_confirmation_is_required(service, writer) -> None:
    plan = _plan(service, writer)
    result = service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token)
    assert result.dry_run and not writer.calls
    with pytest.raises(WritebackConflict, match="confirmed"):
        service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False)


def test_backup_is_taken_from_the_exact_target_and_rollback_is_cas_protected(service, writer) -> None:
    plan = _plan(service, writer)
    applied = service.apply(vendor="rekordbox", source_playlist_id="source", desired_ids=["a", "b"], target=WritebackTarget("live", "/fixture/live.db", "one"), plan_token=plan.plan_token, dry_run=False, confirmed=True)
    assert ("backup", Path("/fixture/live.db")) in writer.calls
    writer.playlists["two"] = ("Set", ["b", "d"])
    reverted = service.rollback(
        vendor="rekordbox", target_mode="live", target_path="/fixture/live.db", target_id="one",
        backup_id=applied.backup_id or "", expected_target_revision=applied.target_revision or "",
        confirmed=True,
    )
    assert reverted.rolled_back and writer.playlists["one"][1] == ["a", "c"]
    assert writer.playlists["two"][1] == ["b", "d"]
    with pytest.raises(WritebackConflict):
        service.rollback(
            vendor="rekordbox", target_mode="live", target_path="/fixture/live.db", target_id="one",
            backup_id=applied.backup_id or "", expected_target_revision=applied.target_revision or "",
            confirmed=True,
        )


def test_wrong_target_path_is_refused_by_production_factory(monkeypatch, tmp_path) -> None:
    from apps.webui.server import playlist_writeback as module
    monkeypatch.setattr(module.paths, "REKORDBOX_LIVE_DB", tmp_path / "live.db")
    with (
        pytest.raises(ValueError, match="refusing target"),
        module.default_writer_factory("rekordbox", "live", tmp_path / "working.db"),
    ):
        pass


def test_production_factory_refuses_unowned_mapping_state(monkeypatch, tmp_path) -> None:
    from apps.webui.server import playlist_writeback as module

    target_path = tmp_path / "MediaLibrary.db"
    target_path.touch()
    monkeypatch.setattr(module.paths, "DJAY_LIVE_DB", target_path)
    with (
        pytest.raises(WritebackUnavailable, match="mapping state database ownership"),
        module.default_writer_factory("djay", "live", target_path),
    ):
        pass


def _assert_connection_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_service_closes_real_bound_mapping_connection_after_success(
    monkeypatch, tmp_path,
) -> None:
    """A successful request releases its exact backend-owned mapping DB."""
    from apps.shared.state import db as state_db
    from apps.shared.state import paths as state_paths
    from apps.webui.server import playlist_writeback as module

    state_path = tmp_path / "custom-state.db"
    decoy_path = tmp_path / "default-state.db"
    for path, vendor_id in ((state_path, "custom-a"), (decoy_path, "decoy-a")):
        with state_db.connect_rw(path) as connection:
            connection.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, created_at, updated_at) "
                "VALUES ('a', 'inferred', 't', 't')"
            )
            connection.execute(
                "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES ('a', 'djay', ?)",
                (vendor_id,),
            )

    target_path = tmp_path / "MediaLibrary.db"
    target_path.touch()
    monkeypatch.setattr(state_paths, "STATE_DB", decoy_path)
    monkeypatch.setattr(module.paths, "DJAY_LIVE_DB", target_path)

    opened_connections: list[sqlite3.Connection] = []
    mapping_paths: list[Path] = []
    bound_factory = module.bind_default_writer_factory(state_path)

    @contextmanager
    def observing_factory(vendor, target_mode, requested_target_path):
        with bound_factory(vendor, target_mode, requested_target_path) as writer:
            assert writer is not None
            opened_connections.append(writer.state_conn)
            mapping_path = writer.state_conn.execute("PRAGMA database_list").fetchone()[2]
            mapping_paths.append(Path(mapping_path).resolve())
            assert writer.state_conn.execute(
                "SELECT vendor_id FROM track_vendor_ids WHERE stable_id = 'a'"
            ).fetchone()[0] == "custom-a"
            yield writer

    service = WritebackService(writer_factory=observing_factory)
    capability = service.capability("djay")

    assert capability.available
    assert mapping_paths == [state_path.resolve()]
    assert len(opened_connections) == 1
    _assert_connection_closed(opened_connections[0])


def test_service_closes_real_bound_mapping_connection_after_exception(
    monkeypatch, tmp_path,
) -> None:
    """A vendor read failure releases mapping state without relying on GC."""
    from apps.shared.state import db as state_db
    from apps.webui.server import playlist_writeback as module

    state_path = tmp_path / "state.db"
    with state_db.connect_rw(state_path):
        pass
    target_path = tmp_path / "MediaLibrary.db"
    target_path.touch()
    monkeypatch.setattr(module.paths, "DJAY_LIVE_DB", target_path)

    opened_connections: list[sqlite3.Connection] = []
    bound_factory = module.bind_default_writer_factory(state_path)

    @contextmanager
    def observing_factory(vendor, target_mode, requested_target_path):
        with bound_factory(vendor, target_mode, requested_target_path) as writer:
            assert writer is not None
            opened_connections.append(writer.state_conn)
            yield writer

    service = WritebackService(writer_factory=observing_factory)
    with pytest.raises(sqlite3.OperationalError, match="no such table: database2"):
        service.targets(
            vendor="djay", target_mode="live", target_path=str(target_path),
        )

    assert len(opened_connections) == 1
    _assert_connection_closed(opened_connections[0])
