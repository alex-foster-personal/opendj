"""Build the generated fixture library red-team pods default to (REDTEAM-05).

A fleet of pods hammering the operator's live rekordbox library unattended is
the failure this module prevents (D5). Every pod that is not given an explicit
real-library path gets a throwaway library built here: a migrated
``state/state.db`` with :data:`FIXTURE_TRACK_COUNT` synthetic tracks, a
rekordbox-shaped ``master.plain.db`` carrying genres and play counts, and real
WAV files under ``music/`` that every track's ``file_path`` resolves to.

Requirements:
    - [if] the library is built twice [then] both trees carry identical content
    - [if] a pod opens the fixture state.db [then] every file_path resolves
    - [if] the build is interrupted part way [then] the next build starts clean

Acceptance tests (one per line, mapped to :mod:`tests.scripts.test_redteam_guardrails`):
    - [if] two builds differ in a track row or a WAV byte [then] deterministic ⛔️
    - [if] a track's file_path is missing from disk [then] the library is unplayable ⛔️

The row shapes come from ``tests/webui/library_wheel_fixtures``, the same
helpers the library-wheel e2e suite seeds with, so the fixture the pods see is
the fixture the e2e tier already treats as a real library.
"""

from __future__ import annotations

import math
import shutil
import sqlite3
import wave
from dataclasses import dataclass
from pathlib import Path

from tests.webui.library_wheel_fixtures import GENRES, _make_master_db, _make_state_db

FIXTURE_TRACK_COUNT = 12
SAMPLE_RATE = 22050
TONE_FRAMES = SAMPLE_RATE // 4
TONE_AMPLITUDE = 6000
TONE_BASE_FREQUENCY = 220.0
DATA_DIR_NAME = "data"
MUSIC_DIR_NAME = "music"
UNMAPPED_TRACK_STRIDE = 5  # every 5th track has no rekordbox mapping at all
_GENRE_IDS = tuple(GENRES)
_TRACK_ID_TEMPLATE = "t-fixture-{:04d}"
_WAV_TEMPLATE = "fixture-{:04d}.wav"
_FIXTURE_STAMP = "2026-01-01T00:00:00Z"
_PLAYLIST = {"playlist_id": "pl-fixture", "name": "Fixture Warmup"}


@dataclass(frozen=True)
class FixtureLibrary:
    """One generated library, rooted at the directory it was written into."""

    root: Path
    data_dir: Path
    music_root: Path
    track_ids: tuple[str, ...]

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state" / "state.db"

    @property
    def master_db(self) -> Path:
        return self.data_dir / "master.plain.db"


def track_id(index: int) -> str:
    """Stable track id for a 1-based fixture index."""
    return _TRACK_ID_TEMPLATE.format(index)


def wav_name(index: int) -> str:
    """Stable WAV file name for a 1-based fixture index."""
    return _WAV_TEMPLATE.format(index)


def _is_mapped(index: int) -> bool:
    """Every 5th track is deliberately absent from the vendor mapping."""
    return index % UNMAPPED_TRACK_STRIDE != 0


def _write_tone(path: Path, frequency: float) -> None:
    """Write one short deterministic sine WAV, so the file is a real playable file."""
    frames = bytearray()
    for index in range(TONE_FRAMES):
        sample = int(TONE_AMPLITUDE * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


def _seed_tables(state_db: Path, master_db: Path, wav_paths: list[Path]) -> None:
    tracks = [
        {
            "stable_id": track_id(index),
            "title": f"Fixture Track {index:02d}",
            "artists": [f"Fixture Artist {(index % 3) + 1}"],
        }
        for index in range(1, FIXTURE_TRACK_COUNT + 1)
    ]
    vendor_ids = {
        track_id(index): f"v-fixture-{index:03d}"
        for index in range(1, FIXTURE_TRACK_COUNT + 1)
        if _is_mapped(index)
    }
    _make_state_db(
        state_db,
        tracks=tracks,
        memberships=[
            {"playlist_id": _PLAYLIST["playlist_id"], "stable_id": track_id(index)}
            for index in range(1, FIXTURE_TRACK_COUNT + 1, 2)
        ],
        playlists=[_PLAYLIST],
        vendor_ids=vendor_ids,
    )
    _make_master_db(
        master_db,
        content=[
            {
                "vendor_id": vendor_id,
                "genre_id": _GENRE_IDS[index % len(_GENRE_IDS)],
                "play_count": index,
            }
            for index, vendor_id in enumerate(sorted(vendor_ids.values()), start=1)
        ],
    )
    # The two columns the shared seeder does not know about, and the reason a
    # pod can load a deck at all: without file_path the track exists and plays
    # nothing, which is the fault class REDTEAM-07 attacks.
    conn = sqlite3.connect(str(state_db))
    try:
        for index, wav_path in enumerate(wav_paths, start=1):
            conn.execute(
                "UPDATE tracks SET file_path = ?, duration_ms = ? WHERE stable_id = ?",
                (str(wav_path), round(TONE_FRAMES * 1000 / SAMPLE_RATE), track_id(index)),
            )
        conn.commit()
    finally:
        conn.close()


def build_fixture_library(root: Path) -> FixtureLibrary:
    """Wipe ``root`` and write a fresh generated library into it."""
    if root.exists():
        shutil.rmtree(root)
    data_dir = root / DATA_DIR_NAME
    state_dir = data_dir / "state"
    music_root = data_dir / MUSIC_DIR_NAME
    state_dir.mkdir(parents=True)
    music_root.mkdir()

    wav_paths = [music_root / wav_name(index) for index in range(1, FIXTURE_TRACK_COUNT + 1)]
    for index, wav_path in enumerate(wav_paths, start=1):
        _write_tone(wav_path, TONE_BASE_FREQUENCY * index)
    _seed_tables(state_dir / "state.db", data_dir / "master.plain.db", wav_paths)
    return FixtureLibrary(
        root=root,
        data_dir=data_dir,
        music_root=music_root,
        track_ids=tuple(track_id(index) for index in range(1, FIXTURE_TRACK_COUNT + 1)),
    )
