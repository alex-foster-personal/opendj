"""Build the throwaway library for the Cmd K pick-race browser gate (CMDK-16, #6091).

No mocked data: TRACK_COUNT real, distinct on-disk wavs written through
StateWriter, served by the real engine on loopback, with ``cmdk.v2`` turned on
in the data dir's own ``feature-flags.json``.

Why so many files: a listing stats at most 16 paths per request (PERF-RB-01)
and hands the rest to the background refresher, so a row's first listing
serves it ``AVAILABILITY_PENDING``. That is the row a Cmd K pick must settle
through rb-meta before it loads, and the wait the gate holds open. Every track
gets its own file because the stat budget is per unique path; one shared file
would be stat'ed once and every row would read present.

Why the fillers: the library table lists its first page
(``LIBRARY_BOOT_FIRST_PAGE_SIZE``, 100 rows) before the bar's index request,
and the refresher can settle those rows in between (CI, Wed 14 Oct 2026: the
table page served 24 pending, the index 0.8 s later served 0). So FILLER_COUNT
rows that sort first by stable id, title and insertion fill that page plus the
index's own 16-stat budget, and every "Pick Race" row reaches the engine first
through the index, pending, however fast the refresher is.

Usage::

    uv run --no-sync python apps/webui/frontend/tests/e2e/support/cmdk_pick_race_fixture.py \\
        --data-dir /abs/path/to/cmdk-pick-race-data
"""

from __future__ import annotations

import argparse
import array
import json
import math
import shutil
import sys
import wave
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[6]
# Run BY PATH (see playlist_switch_fixture.py): sys.path[0] is this directory.
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.shared.state import db as state_db  # noqa: E402
from apps.shared.state import schema as state_schema  # noqa: E402
from apps.shared.state.writer import StateWriter  # noqa: E402

TRACK_COUNT: int = 40
#: The table's first page (LIBRARY_BOOT_FIRST_PAGE_SIZE) plus one request's stat budget.
FILLER_COUNT: int = 100 + 16
FILLER_SECONDS: float = 0.25
PLAYLIST_ID: str = "pl-cmdk-pick-race"
PLAYLIST_NAME: str = "Pick race"
AUDIO_SUBDIR: str = "fixture-audio"
SAMPLE_RATE_HZ: int = 22_050
SECONDS: float = 4.0
#: The flags this gate needs on, written as the data dir's own override file.
FLAGS: dict[str, bool] = {"cmdk.v2": True, "cmdk.v2.preview": False}
#: The one directory this builder may wipe: the e2e fixtures dir's own pick-race data dir,
#: which playwright.cmdk-pick-race.config.ts passes as --data-dir.
FIXTURES_ROOT: Path = Path(__file__).resolve().parents[1] / "fixtures"
DATA_DIR_NAME: str = "cmdk-pick-race-data"


def checked_data_dir(data_dir: Path) -> Path:
    """The data dir to rebuild, or SystemExit for any other path.

    The builder wipes it with ``shutil.rmtree``, so only the named fixture dir
    under the e2e fixtures root is accepted: a typo, ``/``, a repository or a
    home directory is refused before anything is removed (Sol r4278366173).
    """
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {data_dir}")
    resolved = data_dir.resolve()
    expected = FIXTURES_ROOT / DATA_DIR_NAME
    if resolved != expected:
        raise SystemExit(f"[ERROR] --data-dir must be {expected}, got {data_dir}; refusing to wipe it")
    return resolved


def track_title(index: int) -> str:
    """Invented, searchable titles: "Pick Race 00" .. "Pick Race 39"."""
    return f"Pick Race {index:02d}"


def _write_tone(path: Path, index: int, seconds: float = SECONDS) -> None:
    """A quiet sine at a per-track pitch, so no two files share bytes."""
    frames = int(SAMPLE_RATE_HZ * seconds)
    hz = 220.0 + 7.0 * index
    samples = array.array(
        "h",
        (int(3000 * math.sin(2 * math.pi * hz * n / SAMPLE_RATE_HZ)) for n in range(frames)),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(samples.tobytes())


def build(data_dir: Path) -> None:
    data_dir = checked_data_dir(data_dir)
    # Always fresh: the gate needs a cold path-availability index.
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    (data_dir / "feature-flags.json").write_text(json.dumps(FLAGS), encoding="utf-8")
    state_db_path = data_dir / "state" / "state.db"
    state_db_path.parent.mkdir(parents=True)
    with state_db.connect_rw(state_db_path) as created:
        state_schema.apply_migrations(created)
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="cmdk-pick-race-fixture")
    stable_ids: list[str] = []
    try:
        # Fillers first: they sort before every race row by stable id, title and insertion.
        for index in range(FILLER_COUNT):
            audio = data_dir / AUDIO_SUBDIR / f"filler-{index:03d}.wav"
            _write_tone(audio, 1000 + index, FILLER_SECONDS)
            writer.upsert_track(
                stable_id=f"cmdk-fill-{index:03d}",
                stable_id_tier="inferred",
                title=f"Filler {index:03d}",
                artists=[],
                album=None,
                isrc=None,
                duration_ms=int(FILLER_SECONDS * 1000),
                file_path=str(audio.resolve()),
            )
        for index in range(TRACK_COUNT):
            audio = data_dir / AUDIO_SUBDIR / f"pick-race-{index:02d}.wav"
            _write_tone(audio, index)
            stable_id = f"cmdk-race-{index:02d}"
            stable_ids.append(stable_id)
            writer.upsert_track(
                stable_id=stable_id,
                stable_id_tier="inferred",
                title=track_title(index),
                artists=[],
                album=None,
                isrc=None,
                duration_ms=int(SECONDS * 1000),
                file_path=str(audio.resolve()),
            )
        writer.insert_playlist(
            playlist_id=PLAYLIST_ID,
            name=PLAYLIST_NAME,
            vendor="fixture",
            vendor_pl_id=PLAYLIST_ID,
        )
        writer.set_playlist_memberships(PLAYLIST_ID, stable_ids)
    finally:
        writer.close()
        conn.close()
    verify = state_db.open_ro(state_db_path)
    try:
        count = verify.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        verify.close()
    if count != TRACK_COUNT + FILLER_COUNT:
        raise SystemExit(f"[ERROR] expected {TRACK_COUNT + FILLER_COUNT} tracks, found {count} in {state_db_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cmdk_pick_race_fixture", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, help="absolute path of the throwaway data dir")
    parser.add_argument("--home", help="sandbox HOME the engine runs with, created here so the config needs no shell")
    args = parser.parse_args(argv)
    if args.home:
        Path(args.home).mkdir(parents=True, exist_ok=True)
    build(Path(args.data_dir))
    print(
        f"[OK] cmdk pick-race fixture: {TRACK_COUNT} race tracks after {FILLER_COUNT} fillers, flags {FLAGS}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
