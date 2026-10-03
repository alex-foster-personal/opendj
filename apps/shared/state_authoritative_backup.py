"""Per-machine local backup and selective restore for ADR-0022 authoritative tables.

``hub_backup.py`` protects authoritative rows only after a machine has synced to
the hub. This module closes the unenrolled/offline gap: a verified dated copy
of ``state.db`` on the local machine, plus selective table restore so a
corrupted ``tracks`` table does not block recovering ``smartlists``.

CLI::

    python -m apps.shared.state_authoritative_backup backup  --data-dir D [--dest B] [--keep N]
    python -m apps.shared.state_authoritative_backup list    [--dest B]
    python -m apps.shared.state_authoritative_backup verify  --backup F
    python -m apps.shared.state_authoritative_backup restore --backup F --data-dir D --table T
"""

from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.shared import sqlite_verified_copy as svc

BACKUP_PREFIX: str = "state-authoritative-"
DEFAULT_KEEP: int = 14

NOTES_TAGS_FIELDS: frozenset[str] = frozenset({"notes", "tags"})


class StateAuthoritativeBackupError(svc.VerifiedCopyError):
    """A local authoritative backup or selective restore could not be completed."""


@dataclass(frozen=True)
class TableRestoreGroup:
    """One CLI ``--table`` name and the physical tables it replaces."""

    cli_name: str
    physical_tables: tuple[str, ...]
    delete_sql: tuple[str, ...]
    insert_sql: tuple[str, ...]
    count_sql: tuple[str, ...]


# Placeholder in a group's insert SQL for the columns the live table and the
# backup's table share, resolved at restore time by ``_shared_columns``.
_SHARED_COLUMNS = "{shared_columns}"


def _full_table_group(name: str) -> TableRestoreGroup:
    delete = (f'DELETE FROM "{name}"',)
    # Named columns, not SELECT *: a backup taken before a column was added
    # (pairings.snapshot_json, PAIR-04) must still restore into the newer
    # table, which fills the missing column with its default.
    insert = (
        f'INSERT INTO main."{name}" ({_SHARED_COLUMNS}) '
        f'SELECT {_SHARED_COLUMNS} FROM restore_src."{name}"',
    )
    count = (f'SELECT COUNT(*) FROM restore_src."{name}"',)
    return TableRestoreGroup(
        cli_name=name,
        physical_tables=(name,),
        delete_sql=delete,
        insert_sql=insert,
        count_sql=count,
    )


RESTORE_REGISTRY: dict[str, TableRestoreGroup] = {
    "pairings": _full_table_group("pairings"),
    "smartlists": _full_table_group("smartlists"),
    "play_orders": TableRestoreGroup(
        cli_name="play_orders",
        physical_tables=("play_order_entries", "play_orders", "play_orders_schema_meta"),
        delete_sql=(
            'DELETE FROM "play_order_entries"',
            'DELETE FROM "play_orders"',
            'DELETE FROM "play_orders_schema_meta"',
        ),
        insert_sql=(
            'INSERT INTO main."play_orders" SELECT * FROM restore_src."play_orders"',
            'INSERT INTO main."play_order_entries" SELECT * FROM restore_src."play_order_entries"',
            'INSERT INTO main."play_orders_schema_meta" '
            'SELECT * FROM restore_src."play_orders_schema_meta"',
        ),
        count_sql=(
            'SELECT COUNT(*) FROM restore_src."play_orders"',
            'SELECT COUNT(*) FROM restore_src."play_order_entries"',
            'SELECT COUNT(*) FROM restore_src."play_orders_schema_meta"',
        ),
    ),
    "track_fields_notes_tags": TableRestoreGroup(
        cli_name="track_fields_notes_tags",
        physical_tables=("track_fields",),
        delete_sql=(
            "DELETE FROM track_fields WHERE field_name IN ('notes', 'tags')",
        ),
        insert_sql=(
            "INSERT INTO main.track_fields "
            "SELECT * FROM restore_src.track_fields "
            "WHERE field_name IN ('notes', 'tags')",
        ),
        count_sql=(
            "SELECT COUNT(*) FROM restore_src.track_fields "
            "WHERE field_name IN ('notes', 'tags')",
        ),
    ),
}


@dataclass(frozen=True)
class StateAuthoritativeBackup:
    path: Path
    taken_at: str
    row_counts: dict[str, int]
    size_bytes: int


# ----- paths -----------------------------------------------------------------


def state_db_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / "state.db"


def default_backup_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "backup" / "state-authoritative"


def list_backups(dest_dir: Path) -> list[Path]:
    return svc.list_backups(dest_dir, prefix=BACKUP_PREFIX)


# ----- backup ----------------------------------------------------------------


def backup_state_db(
    data_dir: Path,
    dest_dir: Path | None = None,
    *,
    keep: int = DEFAULT_KEEP,
    now: datetime | None = None,
) -> StateAuthoritativeBackup:
    """Take, verify and keep one online backup of ``<data_dir>/state/state.db``."""
    if keep < 1:
        raise StateAuthoritativeBackupError(f"--keep must be at least 1, got {keep}")
    source = state_db_path(data_dir)
    if not source.is_file():
        raise StateAuthoritativeBackupError(
            f"no state.db at {source}; is --data-dir the machine's data dir?"
        )
    taken_at = now if now is not None else datetime.now(UTC)
    target_dir = default_backup_dir(data_dir) if dest_dir is None else Path(dest_dir)
    try:
        final = svc.take_verified_backup(
            source,
            target_dir,
            prefix=BACKUP_PREFIX,
            keep=keep,
            now=taken_at,
            missing_source_message=(
                f"no state.db at {source}; is --data-dir the machine's data dir?"
            ),
            empty_backup_message=f"{source} holds no tables; that is not a state.db",
        )
        counts = svc.verify_backup(final)
    except svc.VerifiedCopyError as exc:
        raise StateAuthoritativeBackupError(str(exc)) from exc
    return StateAuthoritativeBackup(
        path=final,
        taken_at=taken_at.isoformat(),
        row_counts=counts,
        size_bytes=final.stat().st_size,
    )


# ----- selective restore -----------------------------------------------------


def _validate_restore_tables(tables: Sequence[str]) -> list[TableRestoreGroup]:
    unknown = sorted(set(tables) - RESTORE_REGISTRY.keys())
    if unknown:
        allowed = ", ".join(sorted(RESTORE_REGISTRY))
        raise StateAuthoritativeBackupError(
            f"unknown --table name(s) {unknown}; allowed: {allowed}"
        )
    return [RESTORE_REGISTRY[name] for name in tables]


def _scoped_live_count(conn: sqlite3.Connection, group: TableRestoreGroup) -> int:
    if group.cli_name == "track_fields_notes_tags":
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM track_fields WHERE field_name IN ('notes', 'tags')"
        ).fetchone()
        return int(count)
    return sum(
        int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in group.physical_tables
    )


def _resolve_shared_columns(
    conn: sqlite3.Connection, group: TableRestoreGroup, insert: str
) -> str:
    if _SHARED_COLUMNS not in insert:
        return insert
    (table,) = group.physical_tables
    live = [row[1] for row in conn.execute(f'PRAGMA main.table_info("{table}")')]
    old = {row[1] for row in conn.execute(f'PRAGMA restore_src.table_info("{table}")')}
    shared = [col for col in live if col in old]
    if not shared:
        raise StateAuthoritativeBackupError(
            f"backup table {table!r} shares no columns with the live table"
        )
    return insert.replace(_SHARED_COLUMNS, ", ".join(f'"{col}"' for col in shared))


def restore_tables(backup: Path, data_dir: Path, tables: Sequence[str]) -> list[str]:
    """Replace named authoritative table groups from ``backup``; leave the rest untouched."""
    groups = _validate_restore_tables(tables)
    try:
        expected_counts = svc.verify_backup(backup)
    except svc.VerifiedCopyError as exc:
        raise StateAuthoritativeBackupError(str(exc)) from exc
    target = state_db_path(data_dir)
    if not target.is_file():
        raise StateAuthoritativeBackupError(f"no live state.db at {target}")

    engine_config = importlib.import_module("apps.engine_core.config")
    engine_lock = importlib.import_module("apps.engine_core.lock")
    EngineConfig = engine_config.EngineConfig
    EngineLock = engine_lock.EngineLock
    EngineLockError = engine_lock.EngineLockError

    lock = EngineLock(
        EngineConfig(data_dir=Path(data_dir)).lock_path,
        role="opendj-state-authoritative-restore",
    )
    try:
        lock.acquire()
    except EngineLockError as exc:
        raise StateAuthoritativeBackupError(
            f"a live engine holds {data_dir}; stop it first. {exc}"
        ) from exc

    restored: list[str] = []
    backup_path = str(Path(backup).resolve())
    conn = sqlite3.connect(target, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("ATTACH DATABASE ? AS restore_src", (backup_path,))
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                for group in groups:
                    for table in group.physical_tables:
                        if table not in expected_counts:
                            raise StateAuthoritativeBackupError(
                                f"backup {backup} has no table {table!r} for --table "
                                f"{group.cli_name!r}"
                            )
                    expected_scope = sum(
                        int(conn.execute(sql).fetchone()[0]) for sql in group.count_sql
                    )
                    for delete in group.delete_sql:
                        conn.execute(delete)
                    for insert in group.insert_sql:
                        conn.execute(_resolve_shared_columns(conn, group, insert))
                    live_scope = _scoped_live_count(conn, group)
                    if live_scope != expected_scope:
                        raise StateAuthoritativeBackupError(
                            f"restore of {group.cli_name}: expected {expected_scope} rows, "
                            f"got {live_scope}"
                        )
                    restored.append(group.cli_name)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
        finally:
            conn.execute("DETACH DATABASE restore_src")
        verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if verdict != ["ok"]:
            raise StateAuthoritativeBackupError(
                f"live state.db failed integrity_check after restore: {verdict[:5]}"
            )
    finally:
        conn.close()
        lock.release()
    return restored


# ----- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.shared.state_authoritative_backup"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="online, verified backup of state.db")
    backup.add_argument("--data-dir", required=True, type=Path)
    backup.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="backup directory (default: <data-dir>/backup/state-authoritative)",
    )
    backup.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    listing = sub.add_parser("list", help="finished backups, newest first")
    listing.add_argument("--dest", required=True, type=Path)
    verify = sub.add_parser("verify", help="integrity_check + row counts of one backup")
    verify.add_argument("--backup", required=True, type=Path)
    restore = sub.add_parser(
        "restore", help="selectively restore authoritative tables from a backup"
    )
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--data-dir", required=True, type=Path)
    restore.add_argument(
        "--table",
        action="append",
        required=True,
        choices=sorted(RESTORE_REGISTRY),
        help="authoritative table group to restore (repeatable)",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _cmd_backup(args: argparse.Namespace) -> None:
    result = backup_state_db(args.data_dir, args.dest, keep=args.keep)
    _emit(asdict(result))
    print(f"[OK] backup {result.path} verified ({sum(result.row_counts.values())} rows)")


def _cmd_list(args: argparse.Namespace) -> None:
    for path in list_backups(args.dest):
        print(path)


def _cmd_verify(args: argparse.Namespace) -> None:
    try:
        counts = svc.verify_backup(args.backup)
    except svc.VerifiedCopyError as exc:
        raise StateAuthoritativeBackupError(str(exc)) from exc
    _emit({"path": args.backup, "row_counts": counts})
    print(f"[OK] {args.backup} integrity ok")


def _cmd_restore(args: argparse.Namespace) -> None:
    restored = restore_tables(args.backup, args.data_dir, args.table)
    print(f"[OK] restored {', '.join(restored)} from {args.backup}")


COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "backup": _cmd_backup,
    "list": _cmd_list,
    "verify": _cmd_verify,
    "restore": _cmd_restore,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        COMMANDS[args.command](args)
    except StateAuthoritativeBackupError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
