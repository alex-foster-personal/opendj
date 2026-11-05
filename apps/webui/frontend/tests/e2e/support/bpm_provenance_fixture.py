"""Root Playwright fixture: one own beatgrid row for All Tracks BPM provenance (LIBUX-34).

The ordinary deckload library has no analysis records, so every ``td.c-bpm`` hover
stops at the missing reason. This builds that library (with playlists, like the
root ``playwright.config.ts`` suite expects), then attaches the committed
round-1 Beat This! artifact to ``FIXTURE_TRACKS[1]`` via the production
``record_from_payload`` + ``upsert_record`` path
(``tests/webui/test_anlz_own_beatgrid._write_own_record``). The grid is a
dependency of the listing hover under test; its beats need not match the
generated sine.

Runs before the engine opens the data dir. Idempotent: the builder is
revision-gated and the upsert is keyed by stable id.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.bpm_provenance_fixture --data-dir /abs/dir
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from apps.webui.frontend.tests.e2e.support import deckload_fixture
from apps.webui.frontend.tests.e2e.support.deckload_fixture import FIXTURE_TRACKS
from tests.webui.test_anlz_own_beatgrid import TRACK_STATIC_OK, _write_own_record

_TARGET_BASENAME = FIXTURE_TRACKS[1].filename


def _stable_id_for_basename(state_db: Path, basename: str) -> str:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    matches = [str(stable_id) for stable_id, file_path in rows if Path(str(file_path)).name == basename]
    if len(matches) != 1:
        raise SystemExit(
            f"[ERROR] expected exactly one live row with basename {basename!r} in {state_db}, "
            f"found {len(matches)}"
        )
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bpm_provenance_fixture")
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute: {data_dir}")
    built = deckload_fixture.main(["--data-dir", str(data_dir), "--seed-playlists"])
    if built != 0:
        return built
    state_db = data_dir / "state" / "state.db"
    stable_id = _stable_id_for_basename(state_db, _TARGET_BASENAME)
    _write_own_record(state_db, stable_id, TRACK_STATIC_OK)
    print(f"[OK] own beatgrid record written for {stable_id} ({_TARGET_BASENAME})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
