"""Repair duplicate tracks sharing ``audio_hash`` through the remap path.

Mini-PRD
--------
* Default invocation is a read-only preview; ``--live`` is the only mutation.
  [if] no flag is passed [then] duplicate pairs are reported and state.db is unchanged.
* Conflicting normalizable ISRCs are not remapped.
  [if] one audio hash has two ISRCs [then] the report counts a conflict and keeps both rows.
* Live repair records loser/survivor pairs and invokes ``prepare_spoke_identity``.
  [if] a live pair is repaired [then] children are moved through the existing remap implementation.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.ids import normalise_isrc
from apps.sync_hub.engine_identity import _invert_pk
from apps.sync_hub.engine_identity_map import (
    ensure_identity_remap_table,
    prepare_spoke_identity,
    record_identity_remap,
)
from apps.sync_hub.protocol import lww_key


def _pairs(conn: sqlite3.Connection) -> tuple[list[tuple[str, str]], int]:
    rows = conn.execute(
        "SELECT audio_hash, stable_id, isrc, updated_at, origin_device_id "
        "FROM tracks WHERE deleted_at IS NULL AND audio_hash IS NOT NULL "
        "AND audio_hash != '' ORDER BY audio_hash, stable_id"
    ).fetchall()
    groups: dict[str, list[tuple[str, str | None, str | None, str | None]]] = {}
    for audio_hash, stable_id, isrc, updated_at, origin in rows:
        groups.setdefault(str(audio_hash), []).append(
            (str(stable_id), normalise_isrc(isrc), updated_at, origin)
        )
    pairs: list[tuple[str, str]] = []
    conflicts = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        if len({item[1] for item in group if item[1]}) > 1:
            conflicts += 1
            continue
        champion = max(
            group,
            key=lambda item: (
                lww_key({"updated_at": item[2], "origin_device_id": item[3]}),
                _invert_pk(item[0]),
            ),
        )[0]
        pairs.extend((stable_id, champion) for stable_id, *_ in group if stable_id != champion)
    return pairs, conflicts


def run(data_dir: Path, *, live: bool) -> tuple[int, int]:
    """Preview or apply audio-hash remaps. Returns ``(pairs, conflicts)``."""
    path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(path) if live else state_db.open_ro(path)
    try:
        pairs, conflicts = _pairs(conn)
        if live:
            ensure_identity_remap_table(conn)
            remap: dict[str, str] = {}
            for loser, survivor in pairs:
                record_identity_remap(conn, remap, loser, survivor)
            conn.commit()
            prepare_spoke_identity(conn)
            conn.commit()
        return len(pairs), conflicts
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.sync_hub.repair_audio_hash")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="persist remaps; default is dry-run")
    args = parser.parse_args()
    pairs, conflicts = run(args.data_dir, live=args.live)
    mode = "live" if args.live else "dry-run"
    print(f"audio_hash repair ({mode}): pairs={pairs} conflicts={conflicts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
