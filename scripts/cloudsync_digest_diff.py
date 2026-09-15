"""Operator CLI: sample row-level digest divergence against a hub.

    python -m scripts.cloudsync_digest_diff --data-dir DIR --hub URL
    python -m scripts.cloudsync_digest_diff --data-dir DIR --hub URL --table tracks --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import digest_diff, protocol
from apps.sync_hub.client_transport_ops import _local_machine_row, state_db_path
from apps.sync_hub.transport import HttpTransport


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--table", action="append", default=None)
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    conn = state_db.open_ro(state_db_path(args.data_dir))
    try:
        local = protocol.sync_digest(conn)
        # hello must carry THIS machine's real row: the hub upserts it into
        # machines, whose platform CHECK and UNIQUE name reject a made-up probe.
        local_machine = _local_machine_row(conn, sync_stamp.local_machine_id(conn))
    finally:
        conn.close()
    channel = HttpTransport(
        args.hub,
        bearer=__import__("apps.sync_hub.spoke_credential", fromlist=["read_credential"]).read_credential(
            args.data_dir
        ),
    )
    hello = channel.post(
        "/api/v1/sync/hello",
        {
            "machine": local_machine.to_wire(),
            "schema_version": 14,
            "wire_version": 4,
            "capabilities": [],
            "machines": [],
        },
    )
    remote = protocol.SyncDigest.from_wire(
        channel.get(
            "/api/v1/sync/digest",
            {"machine_id": hello["hub_machine_id"], "capabilities": "quarantine/v1"},
        )
    )
    tables = args.table or list(local.divergent_tables(remote))
    if not tables:
        print("no divergent tables", file=sys.stderr)
        return 0
    conn = state_db.open_ro(state_db_path(args.data_dir))
    try:
        machine_id = conn.execute("SELECT machine_id FROM machines LIMIT 1").fetchone()[0]
        rows = digest_diff.sample_divergence(
            channel, conn, machine_id, tables, limit=args.limit
        )
    finally:
        conn.close()
    for row in rows:
        print(
            json.dumps(
                {
                    "table": row.table,
                    "pk": list(row.pk),
                    "local_stamp": row.local_stamp,
                    "hub_stamp": row.hub_stamp,
                    "newer_side": row.newer_side,
                    "reason": row.reason,
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
