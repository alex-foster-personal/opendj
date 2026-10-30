"""Fixture library for the transition-in cue browser smoke (AGENT-51).

The generated deckload fixture tracks have no grid on ``/anlz`` (no rekordbox
mapping, no own beatgrid run), and the transition-in route refuses a track
with no downbeats to snap to. So this builds the ordinary fixture library,
then gives its FIRST track (by stable id) an own beatgrid record: a real Beat
This! result from the round-1 artifact (``tests/analysis_beatgrid``), written
through the production ``record_from_payload`` + ``upsert_record`` path that
``(private path)`` uses. The grid is a dependency
of the thing under test (the menu item, the route and the store), so its
beats need not match the generated audio; the smoke only needs downbeats.

Runs before the engine opens the data dir, so it never writes beside a live
engine. Idempotent: the builder is revision-gated and the upsert is keyed by
stable id.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.transition_in_fixture --data-dir /abs/dir
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from apps.webui.frontend.tests.e2e.support import deckload_fixture
from tests.webui.test_anlz_own_beatgrid import TRACK_STATIC_OK, _write_own_record


def _first_track(state_db: Path) -> str:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"[ERROR] the fixture library at {state_db} holds no track")
    return str(row[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transition_in_fixture")
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    built = deckload_fixture.main(["--data-dir", args.data_dir])
    if built != 0:
        return built
    state_db = Path(args.data_dir) / "state" / "state.db"
    stable_id = _first_track(state_db)
    _write_own_record(state_db, stable_id, TRACK_STATIC_OK)
    print(f"[OK] own beatgrid record written for {stable_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
