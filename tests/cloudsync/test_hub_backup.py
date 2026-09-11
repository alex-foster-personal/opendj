"""Hub backup and restore against a real, migrated hub state.db.

Every refusal here is paired with a subject the same instrument accepts, so
a verify that said no to everything would fail the round trip, and one that
said yes to everything would fail the corrupted-file control
(.claude/rules/verification.md).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.engine_core.config import EngineConfig
from apps.engine_core.lock import EngineLock
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import hub_backup

from .conftest import InMemoryAssetS3, live_r2_config

pytestmark = pytest.mark.requirement("CAT-04")

PROBE_ROWS: int = 40
MARKER_ROW: int = 7


def _marker(i: int) -> str:
    return f"idx-marker-{i:03d}"


@pytest.fixture
def hub_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real hub data dir: migrated state.db, the hub's own row, probe rows."""
    monkeypatch.setenv(machine_identity.IS_HUB_ENV, "1")
    data_dir = tmp_path / "opendj-hub"
    conn = state_db.open_rw(hub_backup.hub_state_db(data_dir))
    try:
        machine_identity.register_machine(conn, data_dir=data_dir, name="hub")
        conn.execute("CREATE TABLE backup_probe (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)")
        conn.execute("CREATE INDEX backup_probe_marker ON backup_probe(marker)")
        conn.executemany(
            "INSERT INTO backup_probe(id, marker) VALUES (?, ?)",
            [(i, _marker(i)) for i in range(PROBE_ROWS)],
        )
    finally:
        conn.close()
    return data_dir


def _rows(db: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute("SELECT id, marker FROM backup_probe ORDER BY id"))
    finally:
        conn.close()


def _corrupt_index(db: Path) -> None:
    """Rewrite one key inside the index b-tree so it disagrees with its table."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        (root,) = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'backup_probe_marker'"
        ).fetchone()
    finally:
        conn.close()
    raw = bytearray(db.read_bytes())
    start = (root - 1) * page_size
    page = bytes(raw[start : start + page_size])
    needle = _marker(MARKER_ROW).encode()
    offset = page.find(needle)
    assert offset >= 0, "control: the marker must sit in the index root page"
    raw[start + offset : start + offset + len(needle)] = _marker(999).encode()
    db.write_bytes(bytes(raw))


# ----- backup + restore ------------------------------------------------------------


def test_backup_restore_round_trip_preserves_every_row(hub_data_dir: Path, tmp_path: Path) -> None:
    """if a restored hub DB loses a row, a table, or fails integrity_check then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    assert backup.path.name.startswith("hub-state-") and backup.path.name.endswith("UTC.db")
    assert backup.row_counts["backup_probe"] == PROBE_ROWS
    assert backup.row_counts["machines"] == 1
    target = hub_backup.restore_hub_db(backup.path, tmp_path / "restored-hub")
    assert target == hub_backup.hub_state_db(tmp_path / "restored-hub")
    assert hub_backup.verify_backup(target) == backup.row_counts
    assert _rows(target) == _rows(hub_backup.hub_state_db(hub_data_dir))


def test_backup_is_online_and_excludes_uncommitted_writes(
    hub_data_dir: Path, tmp_path: Path
) -> None:
    """if a backup blocks on, or includes, a writer's uncommitted transaction then broken"""
    writer = sqlite3.connect(hub_backup.hub_state_db(hub_data_dir), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.executemany(
            "INSERT INTO backup_probe(id, marker) VALUES (?, ?)",
            [(1000 + i, "pending") for i in range(5)],
        )
        backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
        writer.execute("COMMIT")
    finally:
        writer.close()
    assert backup.row_counts["backup_probe"] == PROBE_ROWS
    assert len(_rows(hub_backup.hub_state_db(hub_data_dir))) == PROBE_ROWS + 5


def test_corrupted_source_is_refused_and_leaves_no_backup(
    hub_data_dir: Path, tmp_path: Path
) -> None:
    """if a corrupted hub DB yields a backup file under a restorable name then broken"""
    dest = tmp_path / "backups"
    source = hub_backup.hub_state_db(hub_data_dir)
    assert (
        hub_backup.verify_backup(source)["backup_probe"] == PROBE_ROWS
    )  # control: instrument says yes
    _corrupt_index(source)
    with pytest.raises(hub_backup.HubBackupError, match="integrity_check"):
        hub_backup.verify_backup(source)
    with pytest.raises(hub_backup.HubBackupError, match="integrity_check"):
        hub_backup.backup_hub_db(hub_data_dir, dest, keep=3)
    assert sorted(p.name for p in dest.iterdir()) == []


def test_verify_rejects_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    """if verify accepts arbitrary bytes as a hub backup then broken"""
    junk = tmp_path / "hub-state-20260911T000000UTC.db"
    junk.write_bytes(os.urandom(8192))
    with pytest.raises(hub_backup.HubBackupError, match="not a readable sqlite database"):
        hub_backup.verify_backup(junk)


def test_restore_refuses_to_overwrite_an_existing_db(hub_data_dir: Path, tmp_path: Path) -> None:
    """if restore overwrites a data dir that already holds a state.db then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    live = hub_backup.hub_state_db(hub_data_dir)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    with pytest.raises(hub_backup.HubBackupError, match="refusing to overwrite"):
        hub_backup.restore_hub_db(backup.path, hub_data_dir)
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before


def test_restore_refuses_while_an_engine_holds_the_data_dir(
    hub_data_dir: Path, tmp_path: Path
) -> None:
    """if restore writes into a data dir a live engine holds the lock on then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    target = tmp_path / "busy-hub"
    with (
        EngineLock(EngineConfig(data_dir=target).lock_path, role="pytest-engine"),
        pytest.raises(hub_backup.HubBackupError, match="live engine"),
    ):
        hub_backup.restore_hub_db(backup.path, target)
    assert not hub_backup.hub_state_db(target).exists()
    hub_backup.restore_hub_db(backup.path, target)  # control: free lock, same call succeeds
    assert hub_backup.hub_state_db(target).exists()


def test_restore_refuses_a_corrupted_backup(hub_data_dir: Path, tmp_path: Path) -> None:
    """if a backup that fails integrity_check can be restored then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    _corrupt_index(backup.path)
    with pytest.raises(hub_backup.HubBackupError, match="integrity_check"):
        hub_backup.restore_hub_db(backup.path, tmp_path / "restored-hub")
    assert not hub_backup.hub_state_db(tmp_path / "restored-hub").exists()


def _drop_a_row_from_partials(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Fault injection with real sqlite: delete one probe row from every partial
    copy right before it is verified, so the copy genuinely disagrees with its
    source. Returns the partials it damaged, as proof the fault fired."""
    real_verify = hub_backup.verify_backup
    damaged: list[Path] = []

    def verify_after_losing_a_row(path: Path) -> dict[str, int]:
        if Path(path).name.startswith(hub_backup.CFG.PARTIAL_PREFIX):
            conn = sqlite3.connect(path)
            try:
                conn.execute("DELETE FROM backup_probe WHERE id = ?", (MARKER_ROW,))
                conn.commit()
            finally:
                conn.close()
            damaged.append(Path(path))
        return real_verify(path)

    monkeypatch.setattr(hub_backup, "verify_backup", verify_after_losing_a_row)
    return damaged


def test_backup_whose_copy_lost_a_row_is_refused(
    hub_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a backup copy whose row counts differ from its snapshot is kept then broken"""
    dest = tmp_path / "backups"
    damaged = _drop_a_row_from_partials(monkeypatch)
    with pytest.raises(hub_backup.HubBackupError, match="row counts differ"):
        hub_backup.backup_hub_db(hub_data_dir, dest, keep=3)
    assert len(damaged) == 1  # control: the fault reached the copy
    assert sorted(p.name for p in dest.iterdir()) == []  # no final, no partial


def test_restore_whose_copy_lost_a_row_is_refused(
    hub_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a restore whose row counts differ from the backup takes the state.db name then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    target = tmp_path / "restored-hub"
    damaged = _drop_a_row_from_partials(monkeypatch)
    with pytest.raises(hub_backup.HubBackupError, match="row counts differ"):
        hub_backup.restore_hub_db(backup.path, target)
    assert len(damaged) == 1
    assert sorted(p.name for p in hub_backup.hub_state_db(target).parent.iterdir()) == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_restore_refuses_a_stale_sidecar_alone(
    suffix: str, hub_data_dir: Path, tmp_path: Path
) -> None:
    """if restore writes a state.db beside a stale -wal/-shm/-journal sidecar then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    target = tmp_path / "restored-hub"
    sidecar = Path(f"{hub_backup.hub_state_db(target)}{suffix}")
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"stale frames from another db")
    with pytest.raises(hub_backup.HubBackupError, match="refusing to overwrite"):
        hub_backup.restore_hub_db(backup.path, target)
    assert sorted(p.name for p in sidecar.parent.iterdir()) == [sidecar.name]
    assert sidecar.read_bytes() == b"stale frames from another db"


@pytest.mark.parametrize(
    "dir_name", ["with space", "hash#dir", "query?dir", "pct%20dir", "pct%dir"]
)
def test_backup_and_restore_under_uri_special_directories(
    dir_name: str, hub_data_dir: Path, tmp_path: Path
) -> None:
    """if '#', '?', '%' or ' ' in a path breaks verify or writes a stray file then broken"""
    root = tmp_path / "case"
    base = root / dir_name
    backup = hub_backup.backup_hub_db(hub_data_dir, base / "backups", keep=3)
    assert hub_backup.verify_backup(backup.path)["backup_probe"] == PROBE_ROWS
    target = hub_backup.restore_hub_db(backup.path, base / "restored")
    assert hub_backup.verify_backup(target) == backup.row_counts
    # A read-only verify that lost its ?mode=ro creates a file at the cut-off path.
    assert sorted(p.name for p in root.iterdir()) == [dir_name]
    assert sorted(p.name for p in base.iterdir()) == ["backups", "restored"]


def test_backup_file_and_new_dest_dir_are_owner_only(hub_data_dir: Path, tmp_path: Path) -> None:
    """if a backup of the fleet library is readable by other users on the hub host then broken"""
    dest = tmp_path / "fresh-backups"
    backup = hub_backup.backup_hub_db(hub_data_dir, dest, keep=3)
    assert oct(dest.stat().st_mode & 0o777) == oct(hub_backup.CFG.BACKUP_DIR_MODE)
    assert oct(backup.path.stat().st_mode & 0o777) == oct(hub_backup.CFG.BACKUP_FILE_MODE)


def test_keep_retains_only_the_newest_backups(hub_data_dir: Path, tmp_path: Path) -> None:
    """if more than --keep backups survive, or the newest one is pruned, then broken"""
    dest = tmp_path / "backups"
    base = datetime(2026, 9, 11, 3, 30, tzinfo=UTC)
    for day in range(4):
        hub_backup.backup_hub_db(hub_data_dir, dest, keep=2, now=base + timedelta(days=day))
    assert [p.name for p in hub_backup.list_backups(dest)] == [
        "hub-state-20260914T033000UTC.db",
        "hub-state-20260913T033000UTC.db",
    ]


def test_backup_stamp_refuses_a_non_utc_clock() -> None:
    """if a backup is stamped from a local-offset clock then broken"""
    assert (
        hub_backup.backup_file_name(datetime(2026, 9, 11, 3, 30, tzinfo=UTC))
        == "hub-state-20260911T033000UTC.db"
    )
    with pytest.raises(hub_backup.HubBackupError, match="UTC"):
        hub_backup.backup_file_name(
            datetime(2026, 9, 11, 3, 30, tzinfo=timezone(timedelta(hours=1)))
        )


def test_cli_backup_then_restore_into_existing_db_exits_nonzero(
    hub_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if the backup CLI does not exit 0 on success and 1 on a refused restore then broken"""
    dest = tmp_path / "backups"
    assert (
        hub_backup.main(
            ["backup", "--data-dir", str(hub_data_dir), "--dest", str(dest), "--keep", "2"]
        )
        == 0
    )
    assert "[OK] backup" in capsys.readouterr().out
    (newest,) = hub_backup.list_backups(dest)
    assert (
        hub_backup.main(["restore", "--backup", str(newest), "--data-dir", str(hub_data_dir)]) == 1
    )
    assert "refusing to overwrite" in capsys.readouterr().err


# ----- R2 --------------------------------------------------------------------------


def _cloud_config() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct",
        r2_access_key_id="akid",
        r2_secret_access_key="secret",
        state_bucket="music-dj-state",
        audio_bucket="music-dj-audio",
        hostname="hub",
        bind_host="127.0.0.1",
    )


def test_r2_upload_writes_the_verified_bytes_and_never_overwrites(
    hub_data_dir: Path, tmp_path: Path
) -> None:
    """if an upload misses hub-backups/<host>/<file> or overwrites a stamped key then broken"""
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    store = InMemoryAssetS3()
    key = hub_backup.upload_backup_to_r2(backup, _cloud_config(), store, host_label="agentbox")
    assert key == f"hub-backups/agentbox/{backup.path.name}"
    body, _etag = store.store[("music-dj-state", key)]
    assert body == backup.path.read_bytes()
    with pytest.raises(hub_backup.HubBackupError, match="never overwrites"):
        hub_backup.upload_backup_to_r2(backup, _cloud_config(), store, host_label="agentbox")


@pytest.mark.live_r2
def test_live_r2_upload_round_trip(hub_data_dir: Path, tmp_path: Path) -> None:
    """if a real R2 upload of a verified backup does not read back byte-identical then broken"""
    cfg = live_r2_config()
    if cfg is None:
        pytest.skip("live R2 creds absent; run under `doppler run -p general -c dev_personal --`")
    assert cfg is not None  # the gate's isolated mypy cannot see pytest.skip is NoReturn
    pytest.importorskip("boto3", reason="the R2 client needs the cloud extra (boto3)")
    backup = hub_backup.backup_hub_db(hub_data_dir, tmp_path / "backups", keep=3)
    client = asset_store.boto3_asset_client(cfg)
    label = f"pytest-{uuid.uuid4().hex[:12]}"
    key = hub_backup.upload_backup_to_r2(backup, cfg, client, host_label=label)
    try:
        fetched = client.get_object(cfg.state_bucket, key)
        assert fetched is not None and fetched[0] == backup.path.read_bytes()
    finally:
        assert client.delete_object(cfg.state_bucket, key)
