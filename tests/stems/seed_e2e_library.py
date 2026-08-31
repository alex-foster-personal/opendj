"""Seed a THROWAWAY data dir with one real track, for the stems e2e.

Never points at a real library. The Playwright config calls this with a fresh
temp directory, and the e2e enqueues work that WRITES stem bundles -- so it
must not be able to reach the primary checkout's data dir, this lane's data
dir, or any existing bundle.

The state DB is built by the engine's own migration ladder rather than by
hand-rolled DDL: a fixture whose schema is a copy of the real one is a fixture
that drifts, and the first thing it would hide is a schema change breaking the
stems query.

Usage:
  uv run python -m tests.stems.seed_e2e_library --data-dir /tmp/xyz/data

-Claude
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from apps.engine_core.store.schema import apply_migrations

# A stable id that is obviously synthetic if it ever shows up somewhere real.
E2E_STABLE_ID: str = "e2e" + "0" * 37
E2E_TITLE: str = "Stems E2E Sine"
TRACK_SECONDS: float = 3.0


def make_source_audio(path: Path, seconds: float = TRACK_SECONDS) -> None:
    """A real, synthesised source file: test INPUT, not a test artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac", "2", "-b:a", "192k", str(path),
        ],
        check=True,
    )


def stable_id_for_index(index: int) -> str:
    return f"e2e{index:037d}"


def seed(data_dir: Path, tracks: int = 1) -> Path:
    state_db = data_dir / "state" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db)
    try:
        apply_migrations(connection)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for index in range(tracks):
            audio = data_dir / "music" / f"stems-e2e-sine-{index}.mp3"
            make_source_audio(audio)
            connection.execute(
                "INSERT OR REPLACE INTO tracks (stable_id, stable_id_tier, "
                "title, duration_ms, file_path, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    stable_id_for_index(index),
                    "inferred",
                    f"{E2E_TITLE} {index}",
                    int(TRACK_SECONDS * 1000),
                    str(audio),
                    now,
                    now,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return state_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.stems.seed_e2e_library")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--tracks",
        type=int,
        default=1,
        help="how many tracks to seed. The e2e uses several so the progress "
        "bar is on screen long enough to assert against without a race.",
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    if data_dir.exists() and any(data_dir.glob("state/state.db")):
        raise SystemExit(
            f"error: {data_dir} already holds a state.db. This seeder only "
            "ever builds a fresh throwaway dir, so it refuses rather than "
            "risk writing into a real library."
        )
    state_db = seed(data_dir, tracks=args.tracks)
    print(f"seeded {state_db} with {args.tracks} track(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
