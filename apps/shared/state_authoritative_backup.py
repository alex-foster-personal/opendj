"""Per-machine local backup and selective restore for ADR-0022 authoritative tables.

``hub_backup.py`` protects authoritative rows only after a machine has synced to
the hub. This module closes the unenrolled/offline gap: a verified dated copy
of ``state.db`` on the local machine, plus selective table restore so a
corrupted ``tracks`` table does not block recovering ``smartlists``.

CLI::

    python -m apps.shared.state_authoritative_backup backup  --data-dir D [--dest B] [--keep N]
    python -m apps.shared.state_authoritative_backup list    [--dest B] [--keep N] [--json]
    python -m apps.shared.state_authoritative_backup verify  --backup F
    python -m apps.shared.state_authoritative_backup restore --backup F --data-dir D --table T [--dry-run]
    python -m apps.shared.state_authoritative_backup restore --backup F --data-dir D \\
        --table track_fields_notes_tags --preview
    python -m apps.shared.state_authoritative_backup schedule-install --data-dir D [--dry-run]
    python -m apps.shared.state_authoritative_backup schedule-inspect --data-dir D
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from apps.shared import sqlite_verified_copy as svc
from apps.shared.cli_json_emit import emit_json_payload as _emit

BACKUP_PREFIX: str = "state-authoritative-"
RESTORE_AUDIT_NAME: str = "restore-audit.jsonl"
DEFAULT_KEEP: int = 14
PREFLIGHT_PARTIAL_PROBE_SUFFIX: str = "preflight-writability-probe"

NOTES_TAGS_FIELDS: frozenset[str] = frozenset({"notes", "tags"})
_NOTES_TAGS_IN_SQL: str = ", ".join(f"'{name}'" for name in sorted(NOTES_TAGS_FIELDS))

_INSERT_TARGET_TABLE = re.compile(
    r'INSERT\s+INTO\s+main\.(?:"([^"]+)"|(\w+))',
    re.IGNORECASE,
)


class StateAuthoritativeBackupError(svc.VerifiedCopyError):
    """A local authoritative backup or selective restore could not be completed."""


@dataclass(frozen=True)
class OptionalRestoreTable:
    """A whole table a group restores only when the backup holds it.

    For a table newer than some backups: a backup taken before it existed
    restores the rest of the group and leaves the live table as it is. When
    the backup does hold it, ``ensure_live`` creates it in the live state.db
    first (lazily created tables may not exist there yet).
    """

    name: str
    ensure_live: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class TableRestoreGroup:
    """One CLI ``--table`` name and the physical tables it replaces."""

    cli_name: str
    physical_tables: tuple[str, ...]
    delete_sql: tuple[str, ...]
    insert_sql: tuple[str, ...]
    count_sql: tuple[str, ...]
    optional_tables: tuple[OptionalRestoreTable, ...] = ()


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


def _ensure_http_pairings_table(conn: sqlite3.Connection) -> None:
    """Create ``http_pairings`` with the helper the webui pairing writes use.

    Imported at call time, like the engine lock in :func:`restore_tables`:
    apps.shared may not import apps.webui statically (.importlinter,
    shared-is-the-stable-core), and one DDL source keeps the restored table
    identical to the one the daemon creates.
    """
    pairings_sqlite = importlib.import_module("apps.webui.server.pairings_sqlite")
    pairings_sqlite.ensure_http_pairings_table(conn)


RESTORE_REGISTRY: dict[str, TableRestoreGroup] = {
    # PAIR-04: the user-visible pairing records live in http_pairings (what
    # /api/v1/pairings reads); pairings is the CAT-03 graph mirror. Both are
    # restored together so the API cannot keep serving damaged rows.
    "pairings": replace(
        _full_table_group("pairings"),
        optional_tables=(
            OptionalRestoreTable("http_pairings", _ensure_http_pairings_table),
        ),
    ),
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
        physical_tables=("track_fields", "track_field_history"),
        delete_sql=(
            f"DELETE FROM track_fields WHERE field_name IN ({_NOTES_TAGS_IN_SQL})",
            f"DELETE FROM track_field_history WHERE field_name IN ({_NOTES_TAGS_IN_SQL})",
        ),
        insert_sql=(
            "INSERT INTO main.track_fields "
            "SELECT * FROM restore_src.track_fields "
            f"WHERE field_name IN ({_NOTES_TAGS_IN_SQL})",
            f'INSERT INTO main."track_field_history" ({_SHARED_COLUMNS}) '
            f'SELECT {_SHARED_COLUMNS} FROM restore_src."track_field_history" '
            f"WHERE field_name IN ({_NOTES_TAGS_IN_SQL})",
        ),
        count_sql=(
            "SELECT COUNT(*) FROM restore_src.track_fields "
            f"WHERE field_name IN ({_NOTES_TAGS_IN_SQL})",
            "SELECT COUNT(*) FROM restore_src.track_field_history "
            f"WHERE field_name IN ({_NOTES_TAGS_IN_SQL})",
        ),
    ),
}


@dataclass(frozen=True)
class StateAuthoritativeBackup:
    path: Path
    taken_at: str
    row_counts: dict[str, int]
    size_bytes: int


@dataclass(frozen=True)
class RestorePlanEntry:
    """One table group a restore would replace, with its scoped row counts."""

    table: str
    physical_tables: tuple[str, ...]
    backup_rows: int
    live_rows: int


@dataclass(frozen=True)
class NotesTagsRestorePreview:
    """Read-only replacement summary for ``track_fields`` notes and tags."""

    backup: str
    data_dir: str
    live_counts: dict[str, int]
    backup_counts: dict[str, int]
    matches_backup: bool
    value_conflicts: tuple[dict[str, str], ...]
    live_only: tuple[dict[str, str], ...]
    backup_only: tuple[dict[str, str], ...]
    would_replace_live_rows: int
    would_restore_from_backup_rows: int


# ----- paths -----------------------------------------------------------------


def state_db_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / "state.db"


def default_backup_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "backup" / "state-authoritative"


def restore_audit_path(data_dir: Path) -> Path:
    """Data-dir-local JSONL file holding one line per committed selective restore."""
    return default_backup_dir(data_dir) / RESTORE_AUDIT_NAME


def _append_restore_audit(
    data_dir: Path, backup: Path, tables: Sequence[str], restored_at: datetime
) -> None:
    """Append one operator-readable restore record naming backup, tables and timestamp."""
    path = restore_audit_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event": "state_authoritative_restore",
        "restored_at": restored_at.isoformat(),
        "backup": str(Path(backup).resolve()),
        "tables": list(tables),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def list_backups(dest_dir: Path) -> list[Path]:
    return svc.list_backups(dest_dir, prefix=BACKUP_PREFIX)


@dataclass(frozen=True)
class ListedStateAuthoritativeBackup:
    path: Path
    taken_at: str
    age_seconds: int
    retention_rank: int
    retained_under_keep: bool


def _parse_finished_backup_path(path: Path, *, prefix: str = BACKUP_PREFIX) -> datetime:
    name = path.name
    if name.startswith(svc.VerifiedCopyCFG.PARTIAL_PREFIX):
        raise StateAuthoritativeBackupError(
            f"partial backup must not appear in finished list: {path}"
        )
    if not name.startswith(prefix) or not name.endswith(svc.VerifiedCopyCFG.BACKUP_SUFFIX):
        raise StateAuthoritativeBackupError(
            f"not a finished state-authoritative backup: {path}"
        )
    stamp = name[len(prefix) : -len(svc.VerifiedCopyCFG.BACKUP_SUFFIX)]
    try:
        taken = datetime.strptime(stamp, svc.VerifiedCopyCFG.STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise StateAuthoritativeBackupError(
            f"invalid backup timestamp in {path.name}: {exc}"
        ) from exc
    return taken


def describe_backups(
    dest_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
    now: datetime | None = None,
) -> list[ListedStateAuthoritativeBackup]:
    """List finished backups with age and retention eligibility under ``keep``."""
    if keep < 1:
        raise StateAuthoritativeBackupError(f"--keep must be at least 1, got {keep}")
    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        raise StateAuthoritativeBackupError("reference time must be timezone-aware")
    entries: list[ListedStateAuthoritativeBackup] = []
    for rank, path in enumerate(list_backups(dest_dir)):
        taken = _parse_finished_backup_path(path)
        age_seconds = max(0, int((reference - taken).total_seconds()))
        entries.append(
            ListedStateAuthoritativeBackup(
                path=path,
                taken_at=taken.isoformat(),
                age_seconds=age_seconds,
                retention_rank=rank,
                retained_under_keep=rank < keep,
            )
        )
    return entries


# ----- backup ----------------------------------------------------------------


def _nearest_writable_dir_ancestor(dest: Path) -> None:
    """Ensure some existing directory ancestor can receive ``dest.mkdir(parents=True)``."""
    candidate = dest
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise StateAuthoritativeBackupError(
                f"cannot create backup destination {dest}: no existing parent directory"
            )
        candidate = parent
    if candidate.is_file():
        raise StateAuthoritativeBackupError(
            f"backup destination ancestor {candidate} is a file, not a directory"
        )
    if not candidate.is_dir():
        raise StateAuthoritativeBackupError(
            f"backup destination ancestor {candidate} is not a directory"
        )
    if not os.access(candidate, os.W_OK | os.X_OK):
        raise StateAuthoritativeBackupError(
            f"backup destination ancestor {candidate} is not writable"
        )


def _preflight_backup_destination(dest_dir: Path) -> Path:
    """Fail closed before snapshot work when the backup destination is unusable."""
    dest = Path(dest_dir).resolve()
    if dest.is_file():
        raise StateAuthoritativeBackupError(
            f"backup destination {dest} is a file, not a directory"
        )
    if dest.exists():
        if not dest.is_dir():
            raise StateAuthoritativeBackupError(
                f"backup destination {dest} is not a directory"
            )
        if not os.access(dest, os.W_OK | os.X_OK):
            raise StateAuthoritativeBackupError(
                f"backup destination {dest} is not writable"
            )
    else:
        _nearest_writable_dir_ancestor(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True, mode=svc.VerifiedCopyCFG.BACKUP_DIR_MODE)
    except OSError as exc:
        raise StateAuthoritativeBackupError(
            f"cannot create backup destination {dest}: {exc}"
        ) from exc
    probe = dest / f"{svc.VerifiedCopyCFG.PARTIAL_PREFIX}{PREFLIGHT_PARTIAL_PROBE_SUFFIX}"
    try:
        probe.touch(mode=svc.VerifiedCopyCFG.BACKUP_FILE_MODE, exist_ok=False)
    except OSError as exc:
        raise StateAuthoritativeBackupError(
            f"backup destination {dest} cannot create a partial backup file: {exc}"
        ) from exc
    probe.unlink(missing_ok=True)
    return dest


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
    raw_target = default_backup_dir(data_dir) if dest_dir is None else Path(dest_dir)
    target_dir = _preflight_backup_destination(raw_target)
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


def _notes_tags_field_counts(conn: sqlite3.Connection, *, source: str) -> dict[str, int]:
    """Row counts for ``notes`` and ``tags`` in ``main`` or ``restore_src``."""
    if source not in {"main", "restore_src"}:
        raise ValueError(f"unsupported notes/tags count source: {source!r}")
    table = "track_fields" if source == "main" else "restore_src.track_fields"
    counts = {"notes": 0, "tags": 0, "total": 0}
    for field_name, count in conn.execute(
        f"SELECT field_name, COUNT(*) FROM {table} "
        "WHERE field_name IN ('notes', 'tags') GROUP BY field_name"
    ):
        counts[str(field_name)] = int(count)
    counts["total"] = counts["notes"] + counts["tags"]
    return counts


def _notes_tags_rows(conn: sqlite3.Connection, *, source: str) -> dict[tuple[str, str], str]:
    table = "track_fields" if source == "main" else "restore_src.track_fields"
    return {
        (str(stable_id), str(field_name)): str(value_json)
        for stable_id, field_name, value_json in conn.execute(
            f"SELECT stable_id, field_name, value_json FROM {table} "
            "WHERE field_name IN ('notes', 'tags')"
        )
    }


def preview_track_fields_notes_tags_restore(
    backup: Path, data_dir: Path
) -> NotesTagsRestorePreview:
    """Report notes/tags counts and conflicts without mutating live ``state.db``."""
    target = state_db_path(data_dir)
    if not target.is_file():
        raise StateAuthoritativeBackupError(f"no live state.db at {target}")
    try:
        svc.verify_backup(backup)
    except svc.VerifiedCopyError as exc:
        raise StateAuthoritativeBackupError(str(exc)) from exc

    backup_path = str(Path(backup).resolve())
    conn = sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS restore_src", (backup_path,))
        try:
            live_counts = _notes_tags_field_counts(conn, source="main")
            backup_counts = _notes_tags_field_counts(conn, source="restore_src")
            live_rows = _notes_tags_rows(conn, source="main")
            backup_rows = _notes_tags_rows(conn, source="restore_src")
        finally:
            conn.execute("DETACH DATABASE restore_src")
    finally:
        conn.close()

    value_conflicts: list[dict[str, str]] = []
    live_only: list[dict[str, str]] = []
    backup_only: list[dict[str, str]] = []
    all_keys = set(live_rows) | set(backup_rows)
    for key in sorted(all_keys):
        stable_id, field_name = key
        live_val = live_rows.get(key)
        backup_val = backup_rows.get(key)
        if live_val is not None and backup_val is not None:
            if live_val != backup_val:
                value_conflicts.append(
                    {
                        "stable_id": stable_id,
                        "field_name": field_name,
                        "live_value_json": live_val,
                        "backup_value_json": backup_val,
                    }
                )
        elif live_val is not None:
            live_only.append(
                {
                    "stable_id": stable_id,
                    "field_name": field_name,
                    "live_value_json": live_val,
                }
            )
        else:
            assert backup_val is not None
            backup_only.append(
                {
                    "stable_id": stable_id,
                    "field_name": field_name,
                    "backup_value_json": backup_val,
                }
            )

    matches = (
        live_counts == backup_counts
        and not value_conflicts
        and not live_only
        and not backup_only
    )
    return NotesTagsRestorePreview(
        backup=backup_path,
        data_dir=str(Path(data_dir).resolve()),
        live_counts=live_counts,
        backup_counts=backup_counts,
        matches_backup=matches,
        value_conflicts=tuple(value_conflicts),
        live_only=tuple(live_only),
        backup_only=tuple(backup_only),
        would_replace_live_rows=live_counts["total"],
        would_restore_from_backup_rows=backup_counts["total"],
    )


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
        (fields_count,) = conn.execute(
            f"SELECT COUNT(*) FROM track_fields WHERE field_name IN ({_NOTES_TAGS_IN_SQL})"
        ).fetchone()
        (history_count,) = conn.execute(
            f"SELECT COUNT(*) FROM track_field_history WHERE field_name IN ({_NOTES_TAGS_IN_SQL})"
        ).fetchone()
        return int(fields_count) + int(history_count)
    return sum(
        int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in group.physical_tables
    )


def _resolve_shared_columns(
    conn: sqlite3.Connection, _group: TableRestoreGroup, insert: str
) -> str:
    if _SHARED_COLUMNS not in insert:
        return insert
    match = _INSERT_TARGET_TABLE.search(insert)
    if match is None:
        raise StateAuthoritativeBackupError(
            f"cannot resolve shared columns for insert: {insert!r}"
        )
    table = match.group(1) or match.group(2)
    live = [row[1] for row in conn.execute(f'PRAGMA main.table_info("{table}")')]
    old = {row[1] for row in conn.execute(f'PRAGMA restore_src.table_info("{table}")')}
    shared = [col for col in live if col in old]
    if not shared:
        raise StateAuthoritativeBackupError(
            f"backup table {table!r} shares no columns with the live table"
        )
    return insert.replace(_SHARED_COLUMNS, ", ".join(f'"{col}"' for col in shared))


def _restore_optional_tables(
    conn: sqlite3.Connection, group: TableRestoreGroup, expected_counts: dict[str, int]
) -> tuple[int, int]:
    """Restore the optional tables the backup holds; return (expected, live) row counts."""
    expected = live = 0
    for extra in group.optional_tables:
        if extra.name not in expected_counts:
            continue
        extra.ensure_live(conn)
        (source_rows,) = conn.execute(
            f'SELECT COUNT(*) FROM restore_src."{extra.name}"'
        ).fetchone()
        conn.execute(f'DELETE FROM "{extra.name}"')
        # Named columns: a backup older than a column added since restores
        # with that column at its default instead of failing on the count.
        source_cols = ", ".join(
            f'"{row[1]}"'
            for row in conn.execute(f'PRAGMA restore_src.table_info("{extra.name}")')
        )
        conn.execute(
            f'INSERT INTO main."{extra.name}" ({source_cols}) '
            f'SELECT {source_cols} FROM restore_src."{extra.name}"'
        )
        (live_rows,) = conn.execute(f'SELECT COUNT(*) FROM "{extra.name}"').fetchone()
        expected += int(source_rows)
        live += int(live_rows)
    return expected, live


def _require_backup_tables(
    group: TableRestoreGroup, expected_counts: dict[str, int], backup: Path
) -> None:
    """Refuse a group whose required physical tables the backup does not hold."""
    for table in group.physical_tables:
        if table not in expected_counts:
            raise StateAuthoritativeBackupError(
                f"backup {backup} has no table {table!r} for --table {group.cli_name!r}"
            )


def _restore_group(
    conn: sqlite3.Connection,
    group: TableRestoreGroup,
    expected_counts: dict[str, int],
    backup: Path,
) -> None:
    """Replace one group inside the caller's transaction, then check its row counts."""
    _require_backup_tables(group, expected_counts, backup)
    expected_scope = sum(int(conn.execute(sql).fetchone()[0]) for sql in group.count_sql)
    for delete in group.delete_sql:
        conn.execute(delete)
    for insert in group.insert_sql:
        conn.execute(_resolve_shared_columns(conn, group, insert))
    live_scope = _scoped_live_count(conn, group)
    optional_expected, optional_live = _restore_optional_tables(conn, group, expected_counts)
    expected_scope += optional_expected
    live_scope += optional_live
    if live_scope != expected_scope:
        raise StateAuthoritativeBackupError(
            f"restore of {group.cli_name}: expected {expected_scope} rows, got {live_scope}"
        )


def _read_only_uri(path: Path) -> str:
    """Return a SQLite URI that opens ``path`` read-only."""
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def _plan_group(
    conn: sqlite3.Connection,
    group: TableRestoreGroup,
    expected_counts: dict[str, int],
    backup: Path,
) -> RestorePlanEntry:
    """Count the rows one group would replace, without writing anything."""
    _require_backup_tables(group, expected_counts, backup)
    backup_rows = sum(int(conn.execute(sql).fetchone()[0]) for sql in group.count_sql)
    live_rows = _scoped_live_count(conn, group)
    physical = list(group.physical_tables)
    for extra in group.optional_tables:
        if extra.name not in expected_counts:
            continue
        physical.append(extra.name)
        (source_rows,) = conn.execute(
            f'SELECT COUNT(*) FROM restore_src."{extra.name}"'
        ).fetchone()
        backup_rows += int(source_rows)
        live_exists = conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?",
            (extra.name,),
        ).fetchone()
        if live_exists:
            live_rows += int(
                conn.execute(f'SELECT COUNT(*) FROM main."{extra.name}"').fetchone()[0]
            )
    return RestorePlanEntry(
        table=group.cli_name,
        physical_tables=tuple(physical),
        backup_rows=backup_rows,
        live_rows=live_rows,
    )


def plan_restore(
    backup: Path, data_dir: Path, tables: Sequence[str]
) -> list[RestorePlanEntry]:
    """Dry-run :func:`restore_tables`: verify ``backup`` and report scope read-only.

    Both databases are opened with ``mode=ro`` and no transaction is begun, so
    an operator can inspect what a restore would replace while the engine runs.
    """
    groups = _validate_restore_tables(tables)
    try:
        expected_counts = svc.verify_backup(backup)
    except svc.VerifiedCopyError as exc:
        raise StateAuthoritativeBackupError(str(exc)) from exc
    target = state_db_path(data_dir)
    if not target.is_file():
        raise StateAuthoritativeBackupError(f"no live state.db at {target}")
    conn = sqlite3.connect(_read_only_uri(target), uri=True, isolation_level=None)
    try:
        conn.execute("ATTACH DATABASE ? AS restore_src", (_read_only_uri(backup),))
        try:
            return [_plan_group(conn, group, expected_counts, backup) for group in groups]
        except sqlite3.Error as exc:
            raise StateAuthoritativeBackupError(f"restore dry-run failed: {exc}") from exc
        finally:
            conn.execute("DETACH DATABASE restore_src")
    finally:
        conn.close()


def restore_tables(
    backup: Path,
    data_dir: Path,
    tables: Sequence[str],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Replace named authoritative table groups from ``backup``; leave the rest untouched.

    A restore that commits and passes ``integrity_check`` appends one record to
    :func:`restore_audit_path`; a refused or rolled-back restore writes none.
    """
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
                    _restore_group(conn, group, expected_counts, backup)
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
        _append_restore_audit(
            data_dir, backup, restored, now if now is not None else datetime.now(UTC)
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
    listing.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help="newest N backups that retention would keep (evaluative only; does not prune)",
    )
    listing.add_argument("--json", action="store_true", help="structured stdout for agents")
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
    restore.add_argument(
        "--dry-run",
        action="store_true",
        help="verify the backup and print each group's row counts; write nothing",
    )
    restore.add_argument(
        "--preview",
        action="store_true",
        help="read-only notes/tags conflict summary (track_fields_notes_tags only)",
    )
    sched_install = sub.add_parser(
        "schedule-install",
        help="install a daily OS job that runs backup (opt-in)",
    )
    sched_install.add_argument("--data-dir", required=True, type=Path)
    sched_install.add_argument(
        "--python",
        default=None,
        help=(
            "executable for the scheduled job (default: sys.executable; "
            "packaged installs should use <PAYLOAD>/bin/opendj-python, or "
            "runtime/bin/python3 which is rewritten to the launcher)"
        ),
    )
    sched_install.add_argument("--dest", type=Path, default=None)
    sched_install.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    sched_install.add_argument(
        "--kind",
        choices=("systemd", "launchd"),
        default=None,
        help="scheduler backend (default: systemd on Linux, launchd on macOS)",
    )
    sched_install.add_argument(
        "--unit-dir",
        type=Path,
        default=None,
        help="where to write unit files (default: user systemd or LaunchAgents dir)",
    )
    sched_install.add_argument(
        "--dry-run",
        action="store_true",
        help="print the install plan as JSON without writing files",
    )
    sched_inspect = sub.add_parser(
        "schedule-inspect",
        help="show the registered backup schedule for a data dir",
    )
    sched_inspect.add_argument("--data-dir", required=True, type=Path)
    return parser


def _cmd_backup(args: argparse.Namespace) -> None:
    result = backup_state_db(args.data_dir, args.dest, keep=args.keep)
    _emit(asdict(result))
    print(f"[OK] backup {result.path} verified ({sum(result.row_counts.values())} rows)")


def _cmd_list(args: argparse.Namespace) -> None:
    entries = describe_backups(args.dest, keep=args.keep)
    if args.json:
        _emit(
            {
                "dest": args.dest,
                "keep": args.keep,
                "backups": [asdict(entry) for entry in entries],
            }
        )
    else:
        for entry in entries:
            print(
                f"{entry.path}\t{entry.taken_at}\t{entry.age_seconds}\t{entry.retained_under_keep}"
            )
    print(f"[OK] listed {len(entries)} backups")


def _cmd_verify(args: argparse.Namespace) -> None:
    try:
        counts = svc.verify_backup(args.backup)
    except svc.VerifiedCopyError as exc:
        raise StateAuthoritativeBackupError(str(exc)) from exc
    _emit({"path": args.backup, "row_counts": counts})
    print(f"[OK] {args.backup} integrity ok")


def _cmd_restore(args: argparse.Namespace) -> None:
    if args.dry_run:
        plan = plan_restore(args.backup, args.data_dir, args.table)
        _emit({"backup": args.backup, "dry_run": True, "plan": [asdict(e) for e in plan]})
        for entry in plan:
            print(
                f"[DRY-RUN] {entry.table} ({', '.join(entry.physical_tables)}): "
                f"{entry.live_rows} live rows would be replaced by {entry.backup_rows}"
            )
        return
    if args.preview:
        if args.table != ["track_fields_notes_tags"]:
            raise StateAuthoritativeBackupError(
                "--preview is only supported with a single "
                "--table track_fields_notes_tags"
            )
        preview = preview_track_fields_notes_tags_restore(args.backup, args.data_dir)
        _emit(asdict(preview))
        lc = preview.live_counts
        bc = preview.backup_counts
        print(
            "[OK] preview track_fields_notes_tags: "
            f"live notes={lc['notes']} tags={lc['tags']} total={lc['total']}; "
            f"backup notes={bc['notes']} tags={bc['tags']} total={bc['total']}; "
            f"matches_backup={preview.matches_backup}"
        )
        return
    restored = restore_tables(args.backup, args.data_dir, args.table)
    print(f"[OK] restored {', '.join(restored)} from {args.backup}")


def _cmd_schedule_install(args: argparse.Namespace) -> None:
    from apps.shared import state_authoritative_backup_schedule as schedule

    python = args.python if args.python is not None else sys.executable
    plan = schedule.schedule_install(
        data_dir=args.data_dir,
        python=python,
        dest=args.dest,
        keep=args.keep,
        kind=args.kind,
        unit_dir=args.unit_dir,
        dry_run=args.dry_run,
    )
    payload = asdict(plan)
    _emit(payload)
    if args.dry_run:
        print("[OK] schedule-install dry-run (no files written)")
        return
    print(f"[OK] schedule installed for {plan.data_dir} ({plan.kind}, label {plan.label})")
    for cmd in plan.enable_commands:
        print(f"[NEXT] {cmd}")


def _cmd_schedule_inspect(args: argparse.Namespace) -> None:
    from apps.shared import state_authoritative_backup_schedule as schedule

    payload = schedule.schedule_inspect(data_dir=args.data_dir)
    _emit(payload)
    print(f"[OK] schedule inspect {payload['label']}")


COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "backup": _cmd_backup,
    "list": _cmd_list,
    "verify": _cmd_verify,
    "restore": _cmd_restore,
    "schedule-install": _cmd_schedule_install,
    "schedule-inspect": _cmd_schedule_inspect,
}


def main(argv: Sequence[str] | None = None) -> int:
    from apps.shared.cli_subcommand_main import run_subcommand_cli

    return run_subcommand_cli(
        _build_parser(),
        COMMANDS,
        argv=argv,
        error_type=StateAuthoritativeBackupError,
    )


if __name__ == "__main__":
    raise SystemExit(main())
