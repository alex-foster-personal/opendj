"""Build the 1k-track throwaway library for playlist-switch latency bench.

No mocked data: one real on-disk wav, 1,000 track rows and one playlist
written through StateWriter, served by the real engine on loopback.

Usage::

    uv run --no-sync python apps/webui/frontend/tests/e2e/support/playlist_switch_fixture.py \\
        --data-dir /abs/path/to/playlist-switch-latency-data
"""

from __future__ import annotations

import argparse
import array
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[6]
# The bench's webServer runs this file BY PATH (`uv run --no-sync python
# apps/.../playlist_switch_fixture.py`), so sys.path[0] is this directory,
# not the repository root, and CI's isolated venv deliberately does not
# install the project (ci.yml, "Provision isolated Python test environment":
# a fresh interpreter must import from the tree). Without this line the
# engine's webServer died on `ModuleNotFoundError: No module named 'apps'`
# on every shard that reached the bench (run 35613556818, Mon 21 Sep 2026).
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.shared.state import db as state_db  # noqa: E402
from apps.shared.state.writer import StateWriter  # noqa: E402

FIXTURE_REVISION: int = 1
REVISION_MARKER: str = "playlist-switch-fixture-revision.txt"
TRACK_COUNT: int = 1000
PLAYLIST_ID: str = "pl-perf-1k"
PLAYLIST_NAME: str = "Perf 1k"
AUDIO_SUBDIR: str = "fixture-audio"
AUDIO_NAME: str = "playlist-switch-shared.wav"
SAMPLE_RATE_HZ: int = 44_100
CHANNELS: int = 2


def _discard_stale_revision(data_dir: Path) -> None:
    marker = data_dir / REVISION_MARKER
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if current == str(FIXTURE_REVISION):
        return
    for stale in (data_dir / AUDIO_SUBDIR, data_dir / "state"):
        if stale.exists():
            shutil.rmtree(stale)
    data_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{FIXTURE_REVISION}\n", encoding="utf-8")


def _state_cli(data_dir: Path, *args: str) -> None:
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    env.pop("WEB_CONCURRENCY", None)
    command = [
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.shared.state.cli",
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {' '.join(args)} failed with exit code {result.returncode}"
        )


def _write_shared_wav(path: Path, seconds: float = 2.0) -> None:
    frame_count = int(SAMPLE_RATE_HZ * seconds)
    samples = array.array("h", [0] * (frame_count * CHANNELS))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(samples.tobytes())


def _ensure_shared_audio(data_dir: Path) -> Path:
    audio_dir = data_dir / AUDIO_SUBDIR
    audio_path = audio_dir / AUDIO_NAME
    if not audio_path.is_file() or audio_path.stat().st_size <= 44:
        _write_shared_wav(audio_path)
    return audio_path.resolve()


def _seed_library(data_dir: Path, audio_path: Path) -> None:
    state_db_path = data_dir / "state" / "state.db"
    if not state_db_path.is_file():
        _state_cli(data_dir, "init")
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="playlist-switch-fixture")
    try:
        stable_ids: list[str] = []
        file_path = str(audio_path)
        for i in range(TRACK_COUNT):
            stable_id = f"pl-perf-track-{i:04d}"
            stable_ids.append(stable_id)
            writer.upsert_track(
                stable_id=stable_id,
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[],
                album=None,
                isrc=None,
                duration_ms=60_000,
                file_path=file_path,
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

    verify_conn = state_db.open_ro(state_db_path)
    try:
        row_count = verify_conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL"
        ).fetchone()[0]
    finally:
        verify_conn.close()
    if row_count != TRACK_COUNT:
        raise SystemExit(
            f"[ERROR] expected {TRACK_COUNT} tracks, found {row_count} in {state_db_path}"
        )


def build(data_dir: Path) -> None:
    if not data_dir.is_absolute():
        raise SystemExit("[ERROR] --data-dir must be absolute")
    _discard_stale_revision(data_dir)
    audio_path = _ensure_shared_audio(data_dir)
    _seed_library(data_dir, audio_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="playlist_switch_fixture",
        description="Build the playlist-switch latency bench library.",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="absolute path of the throwaway fixture data dir",
    )
    args = parser.parse_args(argv)
    build(Path(args.data_dir))
    print(
        f"[OK] playlist-switch fixture: {TRACK_COUNT} tracks, playlist {PLAYLIST_ID}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
