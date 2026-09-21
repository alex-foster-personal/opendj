"""Backfill ``tracks.content_hash`` for rows ingested before hashing existed.

Rekordbox ingest now hashes the audio file at ingest time (see
``apps.shared.state.ingest.rekordbox``), but any track ingested before that
fix landed -- or ingested while its audio drive was offline -- still has
``content_hash IS NULL``. This CLI re-visits exactly those rows and hashes
whatever audio is actually reachable on **this machine**.

Resolution prefers this machine's local ``track_locations`` row (primary
first), then falls back to ``tracks.file_path`` through the path map.
Another machine's ``track_locations`` row is never consulted. The summary
reports ``resolved_via_location`` and ``resolved_via_track_path`` counts
alongside the honest ``total`` / ``resolvable`` / ``hashed`` /
``unresolvable`` denominators.

Requirements (mini-PRD)
------------------------
1. Refuses to run without an explicit ``--dry-run`` or ``--live`` (repo
   convention -- see ``apps.vocals`` ``from-stems``).
   [if] neither flag is passed [then ⛔️] argparse rejects before any DB
   is opened.
   [if] both flags are passed [then ⛔️] argparse rejects (mutually
   exclusive group).
2. Never mutates the DB during ``--dry-run``: it opens ``state.db``
   read-only (``PRAGMA query_only``), so an accidental write raises
   instead of silently landing.
   [if] ``--dry-run`` runs [then] ``state.db``'s mtime is unchanged.
3. Hashing happens identically in both modes (dry-run previews exactly
   what live would do); only the persistence step is conditional.
   [if] dry-run and live are run back to back on the same seed data
   [then] their printed ``resolvable``/``hashed``/``unresolvable`` counts
   are identical.
4. Writes go through ``StateWriter.upsert_track`` -- the repo's one
   supported mutation surface -- never a raw ``UPDATE`` on ``tracks``.
   [if] ``--live`` hashes a row [then] an ``events`` row records the
   ``track.update``.
5. Honest denominators (project CLAUDE.md): the printed summary never
   states a rate against a denominator that includes rows with no
   resolvable audio. ``resolvable`` is reported explicitly alongside
   ``total`` so the reader can tell them apart.
   [if] 1 of 2 candidate rows resolves to a real file [then] the summary
   reads ``total=2 resolvable=1 hashed=1 unresolvable=1``, never a bare
   "50%".
6. Idempotent: a hashed row drops out of the ``content_hash IS NULL``
   candidate set, so re-running after a live pass is a true no-op for it.
   [if] ``--live`` runs twice back to back [then] the second run's
   ``hashed`` count for that row is 0.

Status: OK ran-script works-as-expected, regression tests in
``tests/shared/state/test_content_hash_backfill.py``.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apps.shared import hashing
from apps.shared.platform_paths import DATA_DIR, PathMap, load_path_map, resolve_asset_path
from apps.shared.state import db as state_db
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp
from apps.shared.state.writer import StateWriter
from apps.sync_hub import capabilities, engine, protocol, spoke_credential
from apps.sync_hub import config as sync_config
from apps.sync_hub.client_transport_ops import _push_in_batches
from apps.sync_hub.transport import API_PREFIX, HttpTransport, HubTransport, SyncTransportError


@dataclasses.dataclass
class BackfillReport:
    """Counters for one backfill invocation. See module docstring R5."""

    total: int = 0
    resolvable: int = 0
    hashed: int = 0
    unresolvable: int = 0
    resolved_via_location: int = 0
    resolved_via_track_path: int = 0
    live: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _try_hash(file_path: str | None, path_map: PathMap) -> str | None:
    """Sha256 the audio at ``file_path`` if it resolves and is readable.

    Returns None for an empty path, a streaming URI, an unmapped path, or
    any OSError while reading (missing file, unplugged volume,
    permissions) -- every one of those is "no resolvable audio", never a
    crash.
    """
    if not file_path:
        return None
    mapped = resolve_asset_path(file_path, path_map=path_map)
    if mapped.resolved is None:
        return None
    try:
        return hashing.sha256_file(mapped.resolved)
    except OSError:
        return None


def _candidate_rows(
    conn: sqlite3.Connection, limit: int | None
) -> list[sqlite3.Row]:
    sql = (
        "SELECT stable_id, stable_id_tier, title, artists_json, album, "
        "isrc, duration_ms, file_path FROM tracks "
        "WHERE content_hash IS NULL AND deleted_at IS NULL ORDER BY stable_id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def _resolve_hash_path(
    conn: sqlite3.Connection,
    stable_id: str,
    track_file_path: str | None,
    machine_id: str,
) -> tuple[str | None, str]:
    """Prefer this machine's local location, else ``tracks.file_path``.

    Returns ``(path, source)`` where ``source`` is ``"location"`` or
    ``"track_path"``. Another machine's ``track_locations`` row is never
    consulted.
    """
    raw, source = state_locations.local_audio_path_raw(
        conn, stable_id, machine_id=machine_id,
    )
    if raw is not None:
        return (raw, source)
    return (track_file_path, "track_path")


def run_backfill(
    data_dir: Path, *, live: bool, limit: int | None = None
) -> BackfillReport:
    """Hash every ``content_hash IS NULL`` track under ``data_dir``.

    ``live=False`` opens the state DB read-only: hashing still runs (so the
    report is an honest preview) but nothing is written. ``live=True``
    opens read-write and persists each resolvable hash through
    :class:`StateWriter`.
    """
    state_db_path = data_dir / "state" / "state.db"
    path_map = load_path_map(data_dir)
    report = BackfillReport(live=live)

    conn = state_db.open_rw(state_db_path) if live else state_db.open_ro(state_db_path)
    conn.row_factory = sqlite3.Row
    writer: StateWriter | None = None
    try:
        if live:
            writer = StateWriter(conn, actor="backfill-content-hash")

        rows = _candidate_rows(conn, limit)
        report.total = len(rows)
        machine_id = sync_stamp.local_machine_id(conn)

        for row in rows:
            raw_path, source = _resolve_hash_path(
                conn, row["stable_id"], row["file_path"], machine_id
            )
            digest = _try_hash(raw_path, path_map)
            if digest is None:
                report.unresolvable += 1
                continue
            report.resolvable += 1
            report.hashed += 1
            if source == "location":
                report.resolved_via_location += 1
            else:
                report.resolved_via_track_path += 1
            if writer is not None:
                writer.upsert_track(
                    stable_id=row["stable_id"],
                    stable_id_tier=row["stable_id_tier"],
                    title=row["title"],
                    artists=json.loads(row["artists_json"]) if row["artists_json"] else [],
                    album=row["album"],
                    isrc=row["isrc"],
                    duration_ms=row["duration_ms"],
                    file_path=row["file_path"],
                    content_hash=digest,
                )
    finally:
        if writer is not None:
            writer.close()
        conn.close()

    return report


def _track_row(conn: sqlite3.Connection, stable_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT stable_id, stable_id_tier, title, artists_json, album, "
        "isrc, duration_ms, file_path, updated_at, origin_device_id "
        "FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()
    return row


def _row_change_from_track(conn: sqlite3.Connection, row: sqlite3.Row, digest: str) -> protocol.RowChange:
    columns = protocol.table_columns(conn, "tracks")
    full = conn.execute(
        f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?",
        (row["stable_id"],),
    ).fetchone()
    if full is None:
        raise RuntimeError(f"tracks row {row['stable_id']!r} disappeared during backfill")
    values = {column: full[column] for column in columns}
    values["content_hash"] = digest
    values["updated_at"] = sync_stamp.canonical_now()
    pk = (str(row["stable_id"]),)
    return protocol.RowChange(table="tracks", pk=pk, values=values, hash_pending=False)


def run_for_hub_backfill(
    data_dir: Path,
    *,
    live: bool,
    limit: int | None = None,
    hub_endpoint: str | None = None,
    transport: HubTransport | None = None,
) -> BackfillReport:
    """Pull the hub hash_pending list, hash local audio, push hashes to hub."""
    effective = sync_config.resolve_config(data_dir)
    hub_url = hub_endpoint or effective.hub_url
    if not hub_url and transport is None:
        raise RuntimeError("CloudSync hub_url is not configured for this data dir")
    bearer = spoke_credential.read_credential(data_dir)
    channel = transport if transport is not None else HttpTransport(hub_url, bearer=bearer)
    report = BackfillReport(live=live)
    state_db_path = data_dir / "state" / "state.db"
    path_map = load_path_map(data_dir)
    conn = state_db.open_rw(state_db_path) if live else state_db.open_ro(state_db_path)
    conn.row_factory = sqlite3.Row
    writer: StateWriter | None = None
    pending_ids: list[str] = []
    cursor: str | None = None
    try:
        while limit is None or len(pending_ids) < limit:
            page_limit = 500 if limit is None else min(500, limit - len(pending_ids))
            params: dict[str, str | list[str]] = {
                "machine_id": sync_stamp.local_machine_id(conn),
                "limit": str(page_limit),
                "capabilities": list(capabilities.THIS_BUILD),
            }
            if cursor:
                params["cursor"] = cursor
            payload = channel.get(f"{API_PREFIX}/hash-pending", params)
            batch = [str(item) for item in payload.get("stable_ids", [])]
            pending_ids.extend(batch)
            cursor = payload.get("next_cursor")
            if not batch or cursor is None:
                break
        if limit is not None:
            pending_ids = pending_ids[: limit]
        report.total = len(pending_ids)
        if live:
            writer = StateWriter(conn, actor="backfill-content-hash-for-hub")
        machine_id = sync_stamp.local_machine_id(conn)
        push_rows: list[protocol.RowChange] = []
        for stable_id in pending_ids:
            row = _track_row(conn, stable_id)
            if row is None:
                report.unresolvable += 1
                continue
            raw_path, source = _resolve_hash_path(
                conn, stable_id, row["file_path"], machine_id
            )
            digest = _try_hash(raw_path, path_map)
            if digest is None:
                report.unresolvable += 1
                continue
            report.resolvable += 1
            report.hashed += 1
            if source == "location":
                report.resolved_via_location += 1
            else:
                report.resolved_via_track_path += 1
            if writer is not None:
                writer.upsert_track(
                    stable_id=row["stable_id"],
                    stable_id_tier=row["stable_id_tier"],
                    title=row["title"],
                    artists=json.loads(row["artists_json"]) if row["artists_json"] else [],
                    album=row["album"],
                    isrc=row["isrc"],
                    duration_ms=row["duration_ms"],
                    file_path=row["file_path"],
                    content_hash=digest,
                )
                push_rows.append(_row_change_from_track(conn, row, digest))
        if push_rows and live:
            _push_in_batches(
                channel,
                machine_id,
                push_rows,
                engine.machines_snapshot(conn),
                batch_rows=200,
            )
    finally:
        if writer is not None:
            writer.close()
        conn.close()
    return report


def _print_summary(report: BackfillReport) -> None:
    mode = "live" if report.live else "dry-run"
    print(f"content_hash backfill ({mode})")
    print(f"  total (content_hash IS NULL):  {report.total}")
    print(f"  resolvable audio (denominator): {report.resolvable}")
    print(f"  hashed:                        {report.hashed}")
    print(f"  unresolvable:                  {report.unresolvable}")
    print(f"  resolved via track_locations:  {report.resolved_via_location}")
    print(f"  resolved via tracks.file_path: {report.resolved_via_track_path}")
    if not report.live and report.resolvable:
        print("  (dry-run: no writes; pass --live to persist these hashes)")


def _nonnegative_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.shared.state.backfill_content_hash",
        description="Backfill tracks.content_hash for rows with no hash on "
        "record (pre-fix ingest, or audio that was offline at ingest time).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help=f"data root holding state/state.db (default: {DATA_DIR})",
    )
    parser.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=None,
        help="max candidate rows to process this run (default: no cap)",
    )
    parser.add_argument(
        "--for-hub",
        action="store_true",
        help="pull hash_pending stable_ids from the configured hub and push hashes",
    )
    parser.add_argument(
        "--hub-endpoint",
        default=None,
        help="override hub URL for --for-hub (default: CloudSync config)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="hash and report only, no writes"
    )
    mode.add_argument(
        "--live", action="store_true", help="hash and persist to tracks.content_hash"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_db_path = args.data_dir / "state" / "state.db"
    if not state_db_path.exists():
        print(f"error: state DB not found at {state_db_path}", file=sys.stderr)
        return 1
    try:
        if args.for_hub:
            report = run_for_hub_backfill(
                args.data_dir,
                live=args.live,
                limit=args.limit,
                hub_endpoint=args.hub_endpoint,
            )
        else:
            report = run_backfill(args.data_dir, live=args.live, limit=args.limit)
    except (RuntimeError, SyncTransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["BackfillReport", "main", "run_backfill", "run_for_hub_backfill"]
