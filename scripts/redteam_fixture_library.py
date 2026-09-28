"""Build the generated fixture library red-team pods default to (REDTEAM-05).

A fleet of pods hammering the operator's live rekordbox library unattended is
the failure this module prevents (D5). Every pod that is not given an explicit
real-library path gets a throwaway library built here: a migrated
``state/state.db`` holding :data:`FIXTURE_TRACK_COUNT` synthetic tracks with
rekordbox vendor ids, a rekordbox-shaped ``master.plain.db`` carrying genres
and play counts, and real WAV files under ``music/`` that every track's
``file_path`` resolves to.

Requirements:
    - [if] the library is built twice [then] both trees carry identical content
    - [if] a pod opens the fixture state.db [then] every file_path resolves
    - [if] a build is interrupted part way [then] the next build starts clean
    - [if] a pod reads a mapped track through the rekordbox readers [then] no
      table or column is missing

Acceptance tests (mapped to :mod:`tests.scripts.test_redteam_guardrails` and
:mod:`tests.scripts.test_redteam_fixture_library`):
    - [if] two builds differ in a track row or a WAV byte [then] deterministic is broken
    - [if] a track's file_path is missing from disk [then] the library is unplayable
    - [if] a mapped track 500s on the listing or rb-meta [then] the fixture
      schema drifted from the readers
    - [if] a fixture table or column is absent from pyrekordbox's model [then]
      it is not rekordbox-shaped

Rows are written through the PRODUCTION writer (``apps.shared.state.writer``),
never through a test helper: this module is imported by
``scripts.redteam_trigger``, which the fleet runs with ``uv run --no-project``
and no dev extra, so a ``pytest`` import anywhere on this path breaks the
trigger before it can even read the kill switch (Codex P1 on PR #3706). The
vendor-side ``master.plain.db`` is generated here for the same reason: it is
rekordbox's schema, not ours, and no production module creates one. Its DDL is
the committed snapshot :data:`REKORDBOX_SCHEMA_PATH`, rendered from
pyrekordbox by :mod:`scripts.redteam_fixture_schema`, which this module must
not import (pyrekordbox is a project dependency).
"""

from __future__ import annotations

import math
import shutil
import sqlite3
import wave
from dataclasses import dataclass
from pathlib import Path

from apps.shared.state.schema import apply_migrations
from apps.shared.state.writer import StateWriter

FIXTURE_TRACK_COUNT = 12
SAMPLE_RATE = 22050
TONE_FRAMES = SAMPLE_RATE // 4
TONE_LENGTH_S = TONE_FRAMES // SAMPLE_RATE  # rekordbox Length is whole seconds
TONE_AMPLITUDE = 6000
TONE_BASE_FREQUENCY = 220.0
DATA_DIR_NAME = "data"
MUSIC_DIR_NAME = "music"
UNMAPPED_TRACK_STRIDE = 5  # every 5th track has no rekordbox mapping at all
REKORDBOX_SCHEMA_PATH = Path(__file__).with_name("redteam_fixture_rekordbox_schema.sql")
REKORDBOX_TIMESTAMP = "2026-01-01 00:00:00.000 +00:00"  # rekordbox's created_at format
FIXTURE_GENRES = (
    ("g-fixture-techno", "Peak Time Techno"),
    ("g-fixture-house", "Jackin House"),
    ("g-fixture-spoken", "Spoken Word Poetry"),
)
_TRACK_ID_TEMPLATE = "t-fixture-{:04d}"
_WAV_TEMPLATE = "fixture-{:04d}.wav"
_STABLE_ID_TIER = "inferred"
_VENDOR = "rekordbox"


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


def _seed_master_db(path: Path, wav_paths: list[Path]) -> None:
    """Write a rekordbox-schema master.plain.db holding the mapped tracks.

    The whole schema comes from :data:`REKORDBOX_SCHEMA_PATH`, so every table
    and column a webui reader selects exists and a mapped track never 500s on
    ``no such column`` / ``no such table``. ``ImagePath`` stays NULL (the
    fixture ships no artwork) and ``djmdCue`` stays empty (no cues).
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(REKORDBOX_SCHEMA_PATH.read_text())
        conn.executemany(
            "INSERT INTO djmdGenre (ID, Name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            [
                (genre_id, name, REKORDBOX_TIMESTAMP, REKORDBOX_TIMESTAMP)
                for genre_id, name in FIXTURE_GENRES
            ],
        )
        rows = [
            (
                f"v-fixture-{index:03d}",
                str(wav_path),
                wav_path.name,
                f"Fixture Track {index:02d}",
                FIXTURE_GENRES[index % len(FIXTURE_GENRES)][0],
                TONE_LENGTH_S,
                index,
                REKORDBOX_TIMESTAMP,
                REKORDBOX_TIMESTAMP,
            )
            for index, wav_path in enumerate(wav_paths, start=1)
            if _is_mapped(index)
        ]
        conn.executemany(
            "INSERT INTO djmdContent (ID, FolderPath, FileNameL, Title, GenreID, Length, "
            "DJPlayCount, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_state_db(state_db: Path, wav_paths: list[Path]) -> None:
    """Write the tracks through the production writer, one WAV path each."""
    conn = sqlite3.connect(str(state_db))
    try:
        apply_migrations(conn)
        writer = StateWriter(conn)
        for index, wav_path in enumerate(wav_paths, start=1):
            stable_id = track_id(index)
            writer.upsert_track(
                stable_id=stable_id,
                stable_id_tier=_STABLE_ID_TIER,
                title=f"Fixture Track {index:02d}",
                artists=[f"Fixture Artist {(index % 3) + 1}"],
                album=None,
                isrc=None,
                duration_ms=round(TONE_FRAMES * 1000 / SAMPLE_RATE),
                file_path=str(wav_path),
            )
            if _is_mapped(index):
                writer.set_vendor_id(stable_id, _VENDOR, f"v-fixture-{index:03d}")
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
    _seed_state_db(state_dir / "state.db", wav_paths)
    _seed_master_db(data_dir / "master.plain.db", wav_paths)
    return FixtureLibrary(
        root=root,
        data_dir=data_dir,
        music_root=music_root,
        track_ids=tuple(track_id(index) for index in range(1, FIXTURE_TRACK_COUNT + 1)),
    )
