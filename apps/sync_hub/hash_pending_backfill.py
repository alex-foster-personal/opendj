"""Pull hub hash_pending stable_ids, hash local audio, push content_hash to hub.

Supersedes: ``python -m apps.shared.state.backfill_content_hash --for-hub``.
Hub pull/hash/push now lives here; the shared module keeps local hashing only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from apps.shared.argparse_types import nonnegative_int as _nonnegative_int
from apps.shared.platform_paths import DATA_DIR, PathMap, load_path_map
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.state.backfill_content_hash import (
    BackfillReport,
    _print_summary,
    _resolve_hash_path,
    _try_hash,
)
from apps.shared.state.writer import StateWriter
from apps.sync_hub import capabilities, engine, protocol, spoke_credential
from apps.sync_hub import config as sync_config
from apps.sync_hub.client_transport_ops import _push_in_batches
from apps.sync_hub.transport import API_PREFIX, HttpTransport, HubTransport, SyncTransportError


def _track_row(conn: sqlite3.Connection, stable_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT stable_id, stable_id_tier, title, artists_json, album, "
        "isrc, duration_ms, file_path, updated_at, origin_device_id "
        "FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()


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
    channel = _resolve_backfill_transport(data_dir, hub_url, transport)
    report = BackfillReport(live=live)
    state_db_path = data_dir / "state" / "state.db"
    path_map = load_path_map(data_dir)
    conn = state_db.open_rw(state_db_path) if live else state_db.open_ro(state_db_path)
    conn.row_factory = sqlite3.Row
    writer: StateWriter | None = None
    try:
        pending_ids = _fetch_pending_ids(channel, conn, limit)
        report.total = len(pending_ids)
        if live:
            writer = StateWriter(conn, actor="backfill-content-hash-for-hub")
        machine_id = sync_stamp.local_machine_id(conn)
        push_rows = _hash_hub_rows(conn, pending_ids, machine_id, path_map, writer, report)
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


def _fetch_pending_ids(channel: HubTransport, conn: sqlite3.Connection, limit: int | None) -> list[str]:
    """Fetch the bounded hash-pending stable-id list from the hub."""
    pending_ids: list[str] = []
    cursor: str | None = None
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
    return pending_ids if limit is None else pending_ids[:limit]


def _hash_hub_rows(
    conn: sqlite3.Connection,
    pending_ids: list[str],
    machine_id: str,
    path_map: PathMap,
    writer: StateWriter | None,
    report: BackfillReport,
) -> list[protocol.RowChange]:
    """Hash local rows and optionally stage their changes for a hub push."""
    push_rows: list[protocol.RowChange] = []
    for stable_id in pending_ids:
        row = _track_row(conn, stable_id)
        if row is None:
            report.unresolvable += 1
            continue
        raw_path, source = _resolve_hash_path(conn, stable_id, row["file_path"], machine_id)
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
    return push_rows


def _resolve_backfill_transport(data_dir: Path, hub_url: str | None, transport: HubTransport | None) -> HubTransport:
    """Return the injected or configured transport after config validation."""
    if transport is not None:
        return transport
    if hub_url is None:
        raise RuntimeError("CloudSync hub_url is not configured for this data dir")
    bearer = spoke_credential.read_credential(data_dir)
    return HttpTransport(hub_url, bearer=bearer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.sync_hub.hash_pending_backfill",
        description="Pull hash_pending stable_ids from the configured hub, hash "
        "local audio, and push content_hash to the hub.",
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
        help="max pending stable_ids to process this run (default: no cap)",
    )
    parser.add_argument(
        "--hub-endpoint",
        default=None,
        help="override hub URL (default: CloudSync config)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="hash and report only, no writes")
    mode.add_argument(
        "--live",
        action="store_true",
        help="hash, persist locally, and push hashes to the hub",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_db_path = args.data_dir / "state" / "state.db"
    if not state_db_path.exists():
        print(f"error: state DB not found at {state_db_path}", file=sys.stderr)
        return 1
    try:
        report = run_for_hub_backfill(
            args.data_dir,
            live=args.live,
            limit=args.limit,
            hub_endpoint=args.hub_endpoint,
        )
    except (RuntimeError, SyncTransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run_for_hub_backfill"]
