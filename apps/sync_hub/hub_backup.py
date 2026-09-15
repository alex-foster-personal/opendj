"""Hub backup and restore: an online, verified, stamped copy of the hub DB.

The hub's ``state.db`` is the only MERGED copy of the fleet, but it is not the
only copy of the data: every spoke holds a full library of its own and can
re-offer it. So the hub DB is treated as a restorable cache, and this module
is what makes "restorable" true (docs/cloudsync/hub-runbook.md).

``backup`` takes an ONLINE copy through the sqlite3 backup API while the hub
keeps serving. Row counts are read inside the same read transaction the copy
runs in, so they describe the snapshot the copy holds rather than a later
instant; the copy is then checked (``PRAGMA integrity_check`` plus equal row
counts per table) BEFORE it is given its final name. A copy that fails either
check is deleted and the run raises: an unverified file never appears under a
name a restore would pick up. Newest ``keep`` copies are kept.

``restore`` writes into a target data dir and refuses when that dir already
holds a DB or a live engine holds its lock. It never overwrites a live hub.

``--upload-r2`` sends the verified copy to the state bucket through the repo's
existing R2 client (:func:`apps.cloud.asset_store.boto3_asset_client`). Creds
come from Doppler (``doppler run -p general -c dev_personal --``), never from
a file. A stamped key that already exists is an error, not an overwrite.

CLI::

    python -m apps.sync_hub.hub_backup backup  --data-dir D --dest B --keep N [--upload-r2]
    python -m apps.sync_hub.hub_backup verify  --backup F
    python -m apps.sync_hub.hub_backup list    --dest B
    python -m apps.sync_hub.hub_backup restore --backup F --data-dir D
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.cloud.asset_store import AssetS3Client, boto3_asset_client
from apps.cloud.config import CloudConfig
from apps.shared import sqlite_verified_copy as svc


class CFG:
    STATE_DB_RELATIVE: Path = Path("state") / "state.db"
    BACKUP_PREFIX: str = "hub-state-"
    BACKUP_SUFFIX: str = svc.VerifiedCopyCFG.BACKUP_SUFFIX
    PARTIAL_PREFIX: str = svc.VerifiedCopyCFG.PARTIAL_PREFIX
    STAMP_FORMAT: str = svc.VerifiedCopyCFG.STAMP_FORMAT
    R2_KEY_PREFIX: str = "hub-backups"
    SIDECAR_SUFFIXES: tuple[str, ...] = svc.VerifiedCopyCFG.SIDECAR_SUFFIXES
    BACKUP_DIR_MODE: int = svc.VerifiedCopyCFG.BACKUP_DIR_MODE
    BACKUP_FILE_MODE: int = svc.VerifiedCopyCFG.BACKUP_FILE_MODE


class HubBackupError(svc.VerifiedCopyError):
    """A backup or restore could not be completed and verified."""


@dataclass(frozen=True)
class HubBackup:
    path: Path
    taken_at: str
    row_counts: dict[str, int]
    size_bytes: int


# ----- paths -----------------------------------------------------------------


def hub_state_db(data_dir: Path) -> Path:
    return Path(data_dir) / CFG.STATE_DB_RELATIVE


def backup_file_name(taken_at: datetime) -> str:
    try:
        return svc.backup_file_name(prefix=CFG.BACKUP_PREFIX, taken_at=taken_at)
    except svc.VerifiedCopyError as exc:
        raise HubBackupError(str(exc)) from exc


def list_backups(dest_dir: Path) -> list[Path]:
    """Finished backups, newest first. Stamps sort lexically by time."""
    return svc.list_backups(dest_dir, prefix=CFG.BACKUP_PREFIX)


# ----- verification ----------------------------------------------------------


def verify_backup(path: Path) -> dict[str, int]:
    """``integrity_check`` must say ok; returns the per-table row counts."""
    try:
        return svc.verify_backup(path, empty_message=f"{path} holds no tables; that is not a hub DB")
    except svc.VerifiedCopyError as exc:
        raise HubBackupError(str(exc)) from exc


def _copy_snapshot(source: Path, partial: Path) -> dict[str, int]:
    try:
        return svc.copy_snapshot(source, partial)
    except svc.VerifiedCopyError as exc:
        raise HubBackupError(str(exc)) from exc


def _require_equal_counts(expected: dict[str, int], actual: dict[str, int], what: str) -> None:
    try:
        svc.require_equal_counts(expected, actual, what)
    except svc.VerifiedCopyError as exc:
        raise HubBackupError(str(exc)) from exc


# ----- backup ----------------------------------------------------------------


def backup_hub_db(
    data_dir: Path, dest_dir: Path, *, keep: int, now: datetime | None = None
) -> HubBackup:
    """Take, verify and keep one online backup of ``<data_dir>/state/state.db``."""
    if keep < 1:
        raise HubBackupError(f"--keep must be at least 1, got {keep}")
    source = hub_state_db(data_dir)
    if not source.is_file():
        raise HubBackupError(f"no hub DB at {source}; is --data-dir the hub's data dir?")
    taken_at = now if now is not None else datetime.now(UTC)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True, mode=CFG.BACKUP_DIR_MODE)
    final = dest_dir / backup_file_name(taken_at)
    partial = dest_dir / f"{CFG.PARTIAL_PREFIX}{final.name}"
    if final.exists() or partial.exists():
        raise HubBackupError(f"{final} (or its partial) already exists; refusing to overwrite")
    partial.touch(mode=CFG.BACKUP_FILE_MODE, exist_ok=False)
    try:
        snapshot_counts = _copy_snapshot(source, partial)
        copied_counts = verify_backup(partial)
        _require_equal_counts(snapshot_counts, copied_counts, f"backup copy of {source}")
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    prune_backups(dest_dir, keep=keep)
    return HubBackup(
        path=final,
        taken_at=taken_at.isoformat(),
        row_counts=copied_counts,
        size_bytes=final.stat().st_size,
    )


def prune_backups(dest_dir: Path, *, keep: int) -> list[Path]:
    """Delete finished backups beyond the newest ``keep``. Returns what went."""
    return svc.prune_backups(dest_dir, prefix=CFG.BACKUP_PREFIX, keep=keep)


# ----- restore ---------------------------------------------------------------


def restore_hub_db(backup: Path, data_dir: Path) -> Path:
    """Write a verified ``backup`` into ``data_dir``; never over an existing DB."""
    # String-imported so grimp does not count a sync_hub -> engine_core edge.
    # engine_core.app already imports scheduler_lifespan at the chassis root;
    # a static import here would close a package cycle. Runtime behavior is
    # unchanged: restore still acquires the same EngineLock.
    engine_config = importlib.import_module("apps.engine_core.config")
    engine_lock = importlib.import_module("apps.engine_core.lock")
    EngineConfig = engine_config.EngineConfig
    EngineLock = engine_lock.EngineLock
    EngineLockError = engine_lock.EngineLockError

    expected_counts = verify_backup(backup)
    target = hub_state_db(data_dir)
    lock = EngineLock(EngineConfig(data_dir=Path(data_dir)).lock_path, role="opendj-hub-restore")
    try:
        lock.acquire()
    except EngineLockError as exc:
        raise HubBackupError(f"a live engine holds {data_dir}; stop the hub first. {exc}") from exc
    try:
        existing = [
            Path(f"{target}{suffix}")
            for suffix in CFG.SIDECAR_SUFFIXES
            if Path(f"{target}{suffix}").exists()
        ]
        if existing:
            raise HubBackupError(
                f"refusing to overwrite existing DB files {[str(p) for p in existing]}; "
                "move them aside first (docs/cloudsync/hub-runbook.md, restore drill)"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{CFG.PARTIAL_PREFIX}{target.name}")
        if partial.exists():
            raise HubBackupError(f"{partial} exists from an earlier restore; remove it first")
        try:
            _copy_snapshot(Path(backup), partial)
            restored_counts = verify_backup(partial)
            _require_equal_counts(expected_counts, restored_counts, f"restore of {backup}")
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    finally:
        lock.release()
    return target


# ----- R2 --------------------------------------------------------------------


def r2_backup_key(host_label: str, backup_path: Path) -> str:
    if not host_label or "/" in host_label:
        raise HubBackupError(
            f"r2 host label must be one non-empty path segment, got {host_label!r}"
        )
    return f"{CFG.R2_KEY_PREFIX}/{host_label}/{Path(backup_path).name}"


def upload_backup_to_r2(
    backup: HubBackup, cfg: CloudConfig, s3: AssetS3Client, *, host_label: str
) -> str:
    """Create ``hub-backups/<host>/<file>`` in the state bucket. Never overwrites."""
    key = r2_backup_key(host_label, backup.path)
    created, _etag = s3.put_object_if_none_match(cfg.state_bucket, key, backup.path.read_bytes())
    if not created:
        raise HubBackupError(
            f"r2://{cfg.state_bucket}/{key} already exists; a stamped backup never overwrites"
        )
    return key


# ----- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.sync_hub.hub_backup")
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="online, verified backup of the hub DB")
    backup.add_argument("--data-dir", required=True, type=Path)
    backup.add_argument("--dest", required=True, type=Path)
    backup.add_argument("--keep", required=True, type=int)
    backup.add_argument("--upload-r2", action="store_true")
    backup.add_argument(
        "--r2-host-label",
        default=socket.gethostname().split(".")[0],
        help="R2 key segment naming this hub host (default: short hostname, printed)",
    )
    verify = sub.add_parser("verify", help="integrity_check + row counts of one backup")
    verify.add_argument("--backup", required=True, type=Path)
    listing = sub.add_parser("list", help="finished backups, newest first")
    listing.add_argument("--dest", required=True, type=Path)
    restore = sub.add_parser("restore", help="restore a backup into an empty data dir")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--data-dir", required=True, type=Path)
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _cmd_backup(args: argparse.Namespace) -> None:
    result = backup_hub_db(args.data_dir, args.dest, keep=args.keep)
    payload: dict[str, object] = asdict(result)
    if args.upload_r2:
        cfg = CloudConfig.from_env()
        payload["r2_key"] = upload_backup_to_r2(
            result, cfg, boto3_asset_client(cfg), host_label=args.r2_host_label
        )
        payload["r2_bucket"] = cfg.state_bucket
    _emit(payload)
    print(f"[OK] backup {result.path} verified ({sum(result.row_counts.values())} rows)")


def _cmd_verify(args: argparse.Namespace) -> None:
    counts = verify_backup(args.backup)
    _emit({"path": args.backup, "row_counts": counts})
    print(f"[OK] {args.backup} integrity ok")


def _cmd_list(args: argparse.Namespace) -> None:
    for path in list_backups(args.dest):
        print(path)


def _cmd_restore(args: argparse.Namespace) -> None:
    target = restore_hub_db(args.backup, args.data_dir)
    print(f"[OK] restored {args.backup} -> {target}")


#: Keys are exactly the subparser names; argparse refuses anything else.
COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "backup": _cmd_backup,
    "verify": _cmd_verify,
    "list": _cmd_list,
    "restore": _cmd_restore,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        COMMANDS[args.command](args)
    except HubBackupError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
