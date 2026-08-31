"""argparse entry for the shared state layer.

Subcommands (built up across Phase 5 plans):

* ``init``       -- create/migrate ``data/state/state.db`` (plan 05-01).
* ``stats``      -- print table counts + source breakdown (plan 05-01/03).
* ``inspect``    -- pretty-print one track as open-dj JSON (plan 05-02).
* ``ingest-rb``  -- Rekordbox -> state.db, dry-run by default (plan 05-03).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import db as state_db
from . import paths as state_paths
from . import schema as state_schema


def _cmd_init(args: argparse.Namespace) -> int:
    """Create / migrate the state DB. Idempotent."""
    target = Path(args.db) if args.db else state_paths.STATE_DB
    existed_before = target.exists()
    with state_db.connect_rw(target) as conn:
        version = state_schema.apply_migrations(conn)
        counts = _table_counts(conn)
    status = "opened" if existed_before else "created"
    print(f"state DB {status} at {target}")
    print(f"schema version: {version} (target {state_schema.SCHEMA_VERSION})")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    """Print table counts + provenance source breakdown + events count."""
    target = Path(args.db) if args.db else state_paths.STATE_DB
    if not target.exists():
        print(
            f"state DB not found at {target}; run "
            f"`python -m apps.shared.state.cli init` first.",
            file=sys.stderr,
        )
        return 1
    with state_db.connect_ro(target) as conn:
        counts = _table_counts(conn)
        tier_counts = _tier_breakdown(conn)
        vendor_counts = _vendor_breakdown(conn)
        source_counts = _source_breakdown(conn)
        events_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        adapters = conn.execute(
            "SELECT adapter_id, last_run_at, last_ok FROM adapters "
            "ORDER BY adapter_id"
        ).fetchall()

    print(f"state DB: {target}")
    print("\n[tables]")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    print("\n[tracks by stable_id_tier]")
    if tier_counts:
        for tier, count in tier_counts.items():
            print(f"  {tier}: {count}")
    else:
        print("  (none)")
    print("\n[track_vendor_ids by vendor]")
    if vendor_counts:
        for vendor, count in vendor_counts.items():
            print(f"  {vendor}: {count}")
    else:
        print("  (none)")
    print("\n[track_fields by source]")
    if source_counts:
        for source, count in source_counts.items():
            print(f"  {source}: {count}")
    else:
        print("  (none)")
    print(f"\n[events] {events_count} row(s)")
    print("\n[adapters]")
    if adapters:
        for adapter_id, last_run_at, last_ok in adapters:
            status = "ok" if last_ok else "fail"
            print(f"  {adapter_id}: last_run={last_run_at or '-'} status={status}")
    else:
        print("  (none)")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Print the open-dj JSON for one stable_id; exit 1 if unknown."""
    from . import provenance

    target = Path(args.db) if args.db else state_paths.STATE_DB
    with state_db.connect_ro(target) as conn:
        doc = provenance.to_open_dj_track(conn, args.stable_id)
    if doc is None:
        print(f"stable_id {args.stable_id!r} not found", file=sys.stderr)
        return 1
    json.dump(doc, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_ingest_rb(args: argparse.Namespace) -> int:
    """Run the Rekordbox ingest adapter (dry-run unless ``--write``)."""
    from .ingest import rekordbox as _rb

    return _rb.run_cli(args)


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in state_schema.TABLES:
        row = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()
        counts[name] = int(row[0]) if row is not None else 0
    return counts


def _tier_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT stable_id_tier, COUNT(*) FROM tracks WHERE deleted_at IS NULL "
        "GROUP BY stable_id_tier ORDER BY stable_id_tier"
    ).fetchall()
    return {tier: int(count) for tier, count in rows}


def _vendor_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT vendor, COUNT(*) FROM track_vendor_ids WHERE deleted_at IS NULL "
        "GROUP BY vendor ORDER BY vendor"
    ).fetchall()
    return {vendor: int(count) for vendor, count in rows}


def _source_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM track_fields WHERE deleted_at IS NULL "
        "GROUP BY source ORDER BY source"
    ).fetchall()
    return {source: int(count) for source, count in rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apps.shared.state.cli",
        description="Shared-state layer admin CLI (open-dj stable_id + "
        "provenance envelope).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"override state DB path (default: {state_paths.STATE_DB})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create/migrate the state DB (idempotent)")
    sub.add_parser("stats", help="print table counts + source breakdown")
    ins = sub.add_parser("inspect", help="print one track as open-dj JSON")
    ins.add_argument("stable_id", help="40-char hex stable_id to inspect")
    ing = sub.add_parser("ingest-rb", help="Rekordbox -> state.db adapter")
    ing.add_argument(
        "--rb-db",
        default=None,
        help="override Rekordbox DB (default: paths.REKORDBOX_WORKING_DB)",
    )
    ing.add_argument(
        "--write",
        action="store_true",
        help="persist changes (default: dry-run, rolled back)",
    )
    ing.add_argument(
        "--stale-ok",
        action="store_true",
        help="allow running against a working copy older than the live DB",
    )
    ing.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most N tracks (debug / smoke-test aid)",
    )
    return parser


_COMMANDS = {
    "init": _cmd_init,
    "stats": _cmd_stats,
    "inspect": _cmd_inspect,
    "ingest-rb": _cmd_ingest_rb,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS[args.cmd]
    try:
        return handler(args)
    except (sqlite3.Error, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
