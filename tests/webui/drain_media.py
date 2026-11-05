"""Real media for the Re-analyze drain tests, consumed through the shipped paths.

The drain tests run a shipped producer on a tracked audio fixture, never a
probe that reports its own result (Codex P1 on PR #5054): the child the
daemon starts decodes and measures real bytes, and each property under test
is read from outside, from the process table and the queue rows.

The producer is ``own_loudness.backfill``, the shipped R128 lane. It is the
one shipped producer a CI shard can run end to end: the fast lane installs
ffmpeg, which is all it needs, while ``own_beatgrid.backfill`` (what
Re-analyze sends) needs a Beat This! checkpoint no runner provisions and
``own_key.backfill`` needs an ``odj-audio`` build only one job makes. The
drain hands the runner whatever backend the route was given, so which
producer runs is not what these tests are about; that one really runs is.

The library rows those tracks need are written by the production folder
import (``importer.run_folder_import``, what setup runs on a folder of new
files), never inserted by a test (Codex P1 on PR #5054): each test copies the
fixture into a music folder and imports it, so a stable_id, a path and a
duration are whatever the shipped ingest made of real bytes.

-Claude
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path

from apps.engine_core.setup import importer
from apps.engine_core.setup.detect import STATE_DB_NAME

REPO: Path = Path(__file__).resolve().parents[2]
#: 16 s of a synthesized 124 BPM drum loop, AAC in MP4. The same fixture
#: tests/analysis_contract/test_pcm_fingerprint.py hydrates, by the same digest.
TRACK: Path = REPO / "apps/audio-engine/tests/fixtures/audio/loop-124bpm-16s-aac.m4a"
#: Version 1 of that fixture. A changed file is a new fixture, never a quiet swap.
TRACK_SHA256: str = "1c17d701d2d39aec459ceac2885e44b8a42271b30e9401cd1c1f484e4f13cab6"
#: Its length, fixed by the digest above.
TRACK_DURATION_S: float = 16.0
#: Copies of the fixture joined into one track longer than admission takes:
#: 360 x 16 s is 96 min, past MAX_ADMITTED_MINUTES (90).
LONG_COPIES: int = 360

LANE: str = "loudness"
BACKEND: str = "own_loudness.backfill"


def state_db(data_dir: Path) -> Path:
    """The state DB the folder import writes under ``data_dir``."""
    return data_dir / "state" / STATE_DB_NAME


def hydrate(folder: Path, name: str) -> Path:
    """A disposable copy of the fixture, checked against its digest."""
    folder.mkdir(parents=True, exist_ok=True)
    track = folder / f"{name}{TRACK.suffix}"
    shutil.copyfile(TRACK, track)
    digest = hashlib.sha256(track.read_bytes()).hexdigest()
    assert digest == TRACK_SHA256, f"{TRACK.name} is not fixture v1: {digest}"
    return track


def hydrate_long(folder: Path, name: str) -> Path:
    """The fixture joined end to end ``LONG_COPIES`` times by ffmpeg, without re-encoding."""
    folder.mkdir(parents=True, exist_ok=True)
    listing = folder / f".{name}.concat.txt"
    listing.write_text(f"file '{TRACK.as_posix()}'\n" * LONG_COPIES, encoding="utf-8")
    track = folder / f"{name}{TRACK.suffix}"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-y",
            str(track),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    listing.unlink()
    return track


def import_tracks(data_dir: Path, names: Sequence[str], *, long_names: Sequence[str] = ()) -> dict[str, str]:
    """Import a copy of the fixture per name through the production folder import.

    ``long_names`` get the 96 min join instead. Returns each name's stable_id,
    as the import assigned it.
    """
    music = data_dir / "music"
    copies = {hydrate(music, name).resolve(): name for name in names}
    copies |= {hydrate_long(music, name).resolve(): name for name in long_names}
    outcome = importer.run_folder_import(data_dir, emit=lambda *_: None, roots=[music])
    assert outcome.tracks_written == len(copies), outcome
    conn = sqlite3.connect(f"file:{state_db(data_dir)}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL").fetchall()
    finally:
        conn.close()
    imported = {copies[Path(path).resolve()]: sid for sid, path in rows if Path(path).resolve() in copies}
    assert sorted(imported) == sorted(copies.values()), (sorted(imported), len(rows))
    return imported
