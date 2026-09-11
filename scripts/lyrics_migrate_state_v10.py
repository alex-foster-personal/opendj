"""Dry-run-first runbook: branch-era state.db onto schema v10 + legacy conversion.

Usage:
    uv run --no-sync python -m scripts.lyrics_migrate_state_v10 --db-path PATH
    uv run --no-sync python -m scripts.lyrics_migrate_state_v10 --db-path PATH --live

Backs up first, renames branch-era lyric tables aside, drops stale indexes,
applies main's ladder to v10 (not SCHEMA_VERSION 11), converts legacy words
into artifacts, and keeps the legacy tables (drop_legacy=False).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.lyrics import legacy_words
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp

KARAOKE_TARGET_VERSION: int = 10
BACKUP_PREFIX: str = "state.db.bak-"
STALE_RED_INDEX: str = "idx_lyric_verdict_red"
STALE_WORD_INDEX: str = "idx_lyric_word_time"
LEGACY_VERDICT: str = legacy_words.LEGACY_VERDICT_TABLE
LEGACY_WORD: str = legacy_words.LEGACY_WORD_TABLE

_POLICY_REMINDER: tuple[str, ...] = (
    "Before cloud produce, PUT sync_policies cells:",
    "  python -m apps.sync_hub policy set --data-dir <data-dir> "
    "--machine self --kind karaoke_words --mode pinned --live",
    "  python -m apps.sync_hub policy set --data-dir <data-dir> "
    "--machine self --kind stem_bundle --mode pinned --live",
)


@dataclass(frozen=True)
class InspectReport:
    """Read-only snapshot of what the runbook would do."""

    schema_version: int
    has_branch_era_verdict: bool
    has_branch_era_word: bool
    has_legacy_verdict: bool
    has_legacy_word: bool
    has_v10_verdict: bool
    stale_red_index: bool
    stale_word_index: bool
    legacy_verdict_rows: int
    live_verdict_rows: int
    already_migrated: bool


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _max_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _is_v10_verdict_table(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "lyric_verdict"):
        return False
    return "pipeline_version" in _column_names(conn, "lyric_verdict")


def _is_branch_era_verdict_table(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "lyric_verdict"):
        return False
    columns = _column_names(conn, "lyric_verdict")
    return "pipeline_version" not in columns


def inspect_db(conn: sqlite3.Connection) -> InspectReport:
    """Summarise the DB state for planning or no-op detection."""
    schema_version = _max_schema_version(conn)
    has_branch_era_verdict = _is_branch_era_verdict_table(conn)
    has_branch_era_word = _table_exists(conn, "lyric_word")
    has_legacy_verdict = _table_exists(conn, LEGACY_VERDICT)
    has_legacy_word = _table_exists(conn, LEGACY_WORD)
    has_v10_verdict = _is_v10_verdict_table(conn)
    legacy_rows = _row_count(conn, LEGACY_VERDICT)
    live_rows = _row_count(conn, "lyric_verdict") if has_v10_verdict else 0
    already_migrated = (
        has_v10_verdict
        and schema_version >= KARAOKE_TARGET_VERSION
        and not has_branch_era_verdict
        and (live_rows > 0 or legacy_rows == 0)
    )
    return InspectReport(
        schema_version=schema_version,
        has_branch_era_verdict=has_branch_era_verdict,
        has_branch_era_word=has_branch_era_word,
        has_legacy_verdict=has_legacy_verdict,
        has_legacy_word=has_legacy_word,
        has_v10_verdict=has_v10_verdict,
        stale_red_index=_index_exists(conn, STALE_RED_INDEX),
        stale_word_index=_index_exists(conn, STALE_WORD_INDEX),
        legacy_verdict_rows=legacy_rows,
        live_verdict_rows=live_rows,
        already_migrated=already_migrated,
    )


def _backup_path(db_path: Path) -> Path:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return db_path.parent / f"{BACKUP_PREFIX}{date}"


def _conversion_preview(
    conn: sqlite3.Connection, data_dir: Path
) -> legacy_words.LegacyMigrationReport | None:
    if not _table_exists(conn, LEGACY_VERDICT):
        return None
    return legacy_words.migrate_legacy_words(
        conn,
        data_dir=data_dir,
        s3=None,
        cfg=None,
        dry_run=True,
        drop_legacy=False,
    )


def _action_prefix(live: bool, verb: str) -> str:
    return verb if live else f"would {verb}"


def _plan_rename_lines(report: InspectReport, *, live: bool) -> list[str]:
    lines: list[str] = []
    if report.has_branch_era_verdict:
        lines.append(
            f"  {_action_prefix(live, 'rename')}: lyric_verdict -> {LEGACY_VERDICT}"
        )
    elif report.has_legacy_verdict:
        lines.append(f"  skip rename: {LEGACY_VERDICT} already present")
    if report.has_branch_era_word:
        lines.append(
            f"  {_action_prefix(live, 'rename')}: lyric_word -> {LEGACY_WORD}"
        )
    elif report.has_legacy_word:
        lines.append(f"  skip rename: {LEGACY_WORD} already present")
    return lines


def _plan_index_lines(report: InspectReport, *, live: bool) -> list[str]:
    lines: list[str] = []
    if report.stale_red_index:
        lines.append(f"  {_action_prefix(live, 'drop')}: {STALE_RED_INDEX}")
    if report.stale_word_index:
        lines.append(f"  {_action_prefix(live, 'drop')}: {STALE_WORD_INDEX}")
    return lines


def _plan_ladder_line(report: InspectReport, *, live: bool) -> str:
    if report.schema_version < KARAOKE_TARGET_VERSION:
        return (
            f"  {_action_prefix(live, 'apply')} ladder: "
            f"{report.schema_version} -> {KARAOKE_TARGET_VERSION}"
        )
    return f"  skip ladder: schema_meta at {report.schema_version}"


def _plan_conversion_lines(
    report: InspectReport,
    conversion: legacy_words.LegacyMigrationReport | None,
    *,
    live: bool,
) -> list[str]:
    if conversion is not None:
        verb = "convert" if live else "would convert"
        lines = [
            f"  {verb}: {conversion.rows_convertible}/{conversion.rows_seen} "
            "legacy verdicts"
        ]
        lines.extend(f"    skip: {failure}" for failure in conversion.failures[:5])
        return lines
    if report.legacy_verdict_rows == 0:
        return ["  no legacy verdict rows to convert"]
    return []


def format_plan(
    report: InspectReport,
    *,
    live: bool,
    backup_path: Path,
    conversion: legacy_words.LegacyMigrationReport | None,
) -> str:
    """Operator-facing plan or no-op message."""
    if report.already_migrated:
        return (
            "[OK] already at schema v10; lyric_verdict populated; nothing to do"
        )
    banner = "[LIVE]" if live else "[DRY-RUN]"
    backup_verb = "backup" if live else "would backup"
    lines: list[str] = [
        banner,
        f"  {backup_verb}: {backup_path}",
        *_plan_rename_lines(report, live=live),
        *_plan_index_lines(report, live=live),
        _plan_ladder_line(report, live=live),
        *_plan_conversion_lines(report, conversion, live=live),
        *_POLICY_REMINDER,
    ]
    if not live:
        lines.append("[..] nothing written; re-run with --live")
    elif report.schema_version < 11:
        lines.append(
            "[..] schema v11 (machine_credentials) left for the next open_rw"
        )
    return "\n".join(lines)


def backup_db(src: Path, dest: Path) -> None:
    """Copy src to dest via sqlite backup API."""
    if dest.exists():
        raise FileExistsError(f"backup already exists: {dest}")
    source = sqlite3.connect(str(src))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(str(dest))
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()


def rename_branch_tables(conn: sqlite3.Connection) -> None:
    """Rename branch-era tables aside; fail if legacy names already taken."""
    if _is_branch_era_verdict_table(conn):
        if _table_exists(conn, LEGACY_VERDICT):
            raise RuntimeError(
                f"{LEGACY_VERDICT} already exists; cannot rename branch-era table"
            )
        conn.execute(f"ALTER TABLE lyric_verdict RENAME TO {LEGACY_VERDICT}")
    if _table_exists(conn, "lyric_word"):
        if _table_exists(conn, LEGACY_WORD):
            raise RuntimeError(
                f"{LEGACY_WORD} already exists; cannot rename branch-era table"
            )
        conn.execute(f"ALTER TABLE lyric_word RENAME TO {LEGACY_WORD}")


def drop_stale_indexes(conn: sqlite3.Connection) -> None:
    """Drop branch-era indexes that follow renamed tables."""
    conn.execute(f"DROP INDEX IF EXISTS {STALE_RED_INDEX}")
    conn.execute(f"DROP INDEX IF EXISTS {STALE_WORD_INDEX}")


def assert_stale_index_gone(conn: sqlite3.Connection) -> None:
    """Abort before the ladder if idx_lyric_verdict_red still exists."""
    if _index_exists(conn, STALE_RED_INDEX):
        raise RuntimeError(
            f"stale index {STALE_RED_INDEX} still attached; "
            "drop it before applying the ladder"
        )


def apply_ladder_to_v10(conn: sqlite3.Connection) -> int:
    """Apply main's ladder up to v10, not SCHEMA_VERSION."""
    current = _max_schema_version(conn)
    if current >= KARAOKE_TARGET_VERSION:
        return current
    original = state_schema.SCHEMA_VERSION
    state_schema.SCHEMA_VERSION = KARAOKE_TARGET_VERSION
    try:
        state_schema.apply_migrations(conn)
    finally:
        state_schema.SCHEMA_VERSION = original
    return _max_schema_version(conn)


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _run_live(db_path: Path) -> int:
    backup_dest = _backup_path(db_path)
    if backup_dest.exists():
        print(f"[ERROR] backup already exists: {backup_dest}", file=sys.stderr)
        return 1
    print("[LIVE]")
    backup_db(db_path, backup_dest)
    conn = state_db.open_rw(db_path, apply_schema=False)
    try:
        rename_branch_tables(conn)
        drop_stale_indexes(conn)
        assert_stale_index_gone(conn)
        apply_ladder_to_v10(conn)
        data_dir = sync_stamp.data_dir_for_connection(conn)
        preview = legacy_words.migrate_legacy_words(
            conn,
            data_dir=data_dir,
            s3=None,
            cfg=None,
            dry_run=True,
            drop_legacy=False,
        )
        print(legacy_words.format_report(preview))
        result = legacy_words.migrate_legacy_words(
            conn,
            data_dir=data_dir,
            s3=None,
            cfg=None,
            dry_run=False,
            drop_legacy=False,
        )
        print(legacy_words.format_report(result))
        if result.failures:
            return 1
    finally:
        conn.close()
    for line in _POLICY_REMINDER:
        print(line)
    print("[..] schema v11 (machine_credentials) left for the next open_rw")
    return 0


def run(db_path: Path, *, live: bool) -> int:
    """Execute the runbook (dry-run or live)."""
    if not db_path.is_file():
        print(f"[ERROR] database not found: {db_path}", file=sys.stderr)
        return 1
    backup_dest = _backup_path(db_path)
    if live:
        conn = state_db.open_rw(db_path, apply_schema=False)
        try:
            report = inspect_db(conn)
            if report.already_migrated:
                print(format_plan(report, live=True, backup_path=backup_dest, conversion=None))
                return 0
        finally:
            conn.close()
        return _run_live(db_path)
    conn = _open_readonly(db_path)
    try:
        report = inspect_db(conn)
        data_dir = db_path.parent.parent if db_path.parent.name == "state" else db_path.parent
        conversion = _conversion_preview(conn, data_dir)
        print(format_plan(report, live=False, backup_path=backup_dest, conversion=conversion))
        if conversion is not None:
            print(legacy_words.format_report(conversion))
    finally:
        conn.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        required=True,
        help="Path to state.db (required for fail-fast safety)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Write changes (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default when --live is omitted)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.live and args.dry_run:
        print("[ERROR] --live and --dry-run are mutually exclusive", file=sys.stderr)
        return 1
    return run(args.db_path, live=args.live)


if __name__ == "__main__":
    sys.exit(main())
