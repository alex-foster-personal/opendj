"""Build the throwaway library for the Cmd K time-to-execute harness (CMDK-9468).

Four real wav tracks on disk, ``cmdk.v2`` and ``cmdk.v2.actions`` on through
the data dir's own ``feature-flags.json``. No filler rows: four paths fit the
16-stat budget so command rows list on the first index.

Usage::

    uv run --no-sync python apps/webui/frontend/tests/e2e/support/cmdk_ttx_fixture.py \\
        --data-dir /abs/path/to/cmdk-ttx-data
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
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.shared.state import db as state_db  # noqa: E402
from apps.shared.state import schema as state_schema  # noqa: E402
from apps.shared.state.writer import StateWriter  # noqa: E402

TRACK_COUNT: int = 4
PLAYLIST_ID: str = "pl-cmdk-ttx"
PLAYLIST_NAME: str = "TTX"
AUDIO_SUBDIR: str = "fixture-audio"
SAMPLE_RATE_HZ: int = 22_050
SECONDS: float = 8.0
FLAGS: dict[str, bool] = {"cmdk.v2": True, "cmdk.v2.actions": True}
FIXTURES_ROOT: Path = Path(__file__).resolve().parents[1] / "fixtures"
DATA_DIR_NAME: str = "cmdk-ttx-data"


def checked_data_dir(data_dir: Path, *, root: Path = FIXTURES_ROOT) -> Path:
    """The data dir to rebuild, or SystemExit for any other path."""
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {data_dir}")
    resolved = data_dir.resolve()
    expected = root / DATA_DIR_NAME
    if resolved != expected:
        raise SystemExit(f"[ERROR] --data-dir must be {expected}, got {data_dir}; refusing to wipe it")
    return resolved


def track_title(index: int) -> str:
    return f"Meadow {index:02d}"


def _write_tone(path: Path, index: int, seconds: float = SECONDS) -> None:
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


def build(data_dir: Path, *, root: Path = FIXTURES_ROOT) -> None:
    data_dir = checked_data_dir(data_dir, root=root)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    (data_dir / "feature-flags.json").write_text(json.dumps(FLAGS), encoding="utf-8")
    state_db_path = data_dir / "state" / "state.db"
    state_db_path.parent.mkdir(parents=True)
    with state_db.connect_rw(state_db_path) as created:
        state_schema.apply_migrations(created)
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="cmdk-ttx-fixture")
    stable_ids: list[str] = []
    try:
        for index in range(TRACK_COUNT):
            audio = data_dir / AUDIO_SUBDIR / f"ttx-{index:02d}.wav"
            _write_tone(audio, index)
            stable_id = f"cmdk-ttx-{index + 1:02d}"
            stable_ids.append(stable_id)
            writer.upsert_track(
                stable_id=stable_id,
                stable_id_tier="inferred",
                title=track_title(index + 1),
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
    if count != TRACK_COUNT:
        raise SystemExit(f"[ERROR] expected {TRACK_COUNT} tracks, found {count} in {state_db_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cmdk_ttx_fixture", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, help="absolute path of the throwaway data dir")
    parser.add_argument("--home", help="sandbox HOME the engine runs with")
    args = parser.parse_args(argv)
    if args.home:
        Path(args.home).mkdir(parents=True, exist_ok=True)
    build(Path(args.data_dir))
    print(f"[OK] cmdk ttx fixture: {TRACK_COUNT} tracks, flags {FLAGS}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
