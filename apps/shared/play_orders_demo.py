"""Smoke-test demo for play-orders CRUD + open-dj serde (Plan 01 §5).

Creates a demo order against a tmp state DB + prints the open-dj JSON.
Not a permanent test -- this is a sanity dial for developers.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from apps.shared.play_orders import (
    add_entry,
    create_play_order,
    load_play_order,
    play_order_to_opendj,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "state.db"
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            po_id = create_play_order(
                conn, playlist_id="pl-demo", name="demo", generated_by="manual"
            )
            add_entry(
                conn, po_id, "open-dj-t-00000001", 0, transition_hint="bpm-match"
            )
            add_entry(
                conn,
                po_id,
                "open-dj-t-00000002",
                1,
                target_key="8A",
                target_tempo=128.0,
                key_sync=True,
                transition_hint="camelot-step-0",
            )
            add_entry(
                conn,
                po_id,
                "open-dj-t-00000003",
                2,
                key_sync=False,
                transition_hint="hardcut",
            )
            po = load_play_order(conn, "pl-demo", "demo")
            doc = play_order_to_opendj(po)
            print(json.dumps(doc, indent=2, sort_keys=True))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
