"""LIBUX-07 remote-audio status: our own file in non-local storage.

A third bucket, distinct from streaming (a service URI) and awaiting-volume
(our file on an unmounted drive). Until a remote copy is recorded on
``track_locations`` (kind=remote, remote_url set), every row is False so the
library glyph stays quiet.

Agent-native: ``python -m apps.shared.remote_status --json`` lists recorded
remote stable_ids (empty when none). HTTP carries the same flag as
``is_remote`` on listing / playlist rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from apps.shared.state import locations

VOLUMES_ROOT = "/Volumes"


def is_remote_audio(
    *,
    is_streaming: bool,
    is_awaiting_volume: bool,
    has_local_audio: bool,
    has_remote_copy: bool,
) -> bool:
    """True only for our own audio held remotely and not locally.

    Streaming service URIs and awaiting-volume paths never count as remote.
    A working local file (the common case) stays unmarked even when a remote
    copy also exists -- local-first, exception-only.
    """
    if is_streaming or is_awaiting_volume or has_local_audio:
        return False
    return has_remote_copy


def mounted_volumes() -> set[str]:
    """Names currently under ``/Volumes``. Empty when unreadable (Linux CI)."""
    try:
        return set(os.listdir(VOLUMES_ROOT))
    except OSError:
        return set()


def is_awaiting_volume(path: str | None, *, mounted: set[str] | None = None) -> bool:
    """True when ``path`` is under an unmounted ``/Volumes/<name>``."""
    if not path or not path.startswith(VOLUMES_ROOT + "/"):
        return False
    remainder = path[len(VOLUMES_ROOT) + 1 :]
    volume = remainder.split("/", 1)[0]
    if not volume:
        return False
    names = mounted_volumes() if mounted is None else mounted
    return volume not in names


def sids_with_remote_copy(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> set[str]:
    """stable_ids that have a recorded ``kind='remote'`` ``remote_url`` row.

    Not machine-scoped: a remote URL is our non-local storage, whoever
    recorded it. ``available`` is ignored because upsert probes ``file_path``,
    which a remote-only row does not have.
    """
    out: set[str] = set()
    if not stable_ids or not locations.locations_table_ready(conn):
        return out
    bind = locations.ID_BIND_BATCH
    sids = list(stable_ids)
    for start in range(0, len(sids), bind):
        batch = sids[start : start + bind]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            "SELECT DISTINCT stable_id FROM track_locations "
            f"WHERE stable_id IN ({placeholders}) AND kind = 'remote' "
            "AND remote_url IS NOT NULL AND remote_url != ''",
            tuple(batch),
        ).fetchall()
        for (stable_id,) in rows:
            out.add(str(stable_id))
    return out


def list_remote_stable_ids(conn: sqlite3.Connection) -> list[str]:
    """Every recorded remote stable_id, sorted. Empty when none exist."""
    if not locations.locations_table_ready(conn):
        return []
    rows = conn.execute(
        "SELECT DISTINCT stable_id FROM track_locations "
        "WHERE kind = 'remote' AND remote_url IS NOT NULL AND remote_url != '' "
        "ORDER BY stable_id"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _state_db_from_args(args: argparse.Namespace) -> Path | None:
    if args.state_db is not None:
        return args.state_db
    if args.data_dir is not None:
        return args.data_dir / "state" / "state.db"
    from apps.shared.state.paths import STATE_DB

    return STATE_DB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.shared.remote_status")
    parser.add_argument("--state-db", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state_db = _state_db_from_args(args)
    ids: list[str] = []
    if state_db is not None and state_db.exists():
        from apps.shared.state import db as state_db_mod

        conn = state_db_mod.open_ro(state_db)
        try:
            ids = list_remote_stable_ids(conn)
        finally:
            conn.close()
    payload = {"remote_stable_ids": ids}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif ids:
        for sid in ids:
            print(sid)
    else:
        print("no remote audio recorded")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
