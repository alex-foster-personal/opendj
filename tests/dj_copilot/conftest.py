"""Shared fixtures for DJ Copilot tests."""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pytest

from apps.shared.harmonic import TrackFeature


_CAMELOT_ORDER = [f"{n}A" for n in range(1, 13)] + [f"{n}B" for n in range(1, 13)]


@pytest.fixture
def po_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def make_tracks(n: int, *, seed: int = 0) -> list[TrackFeature]:
    rng = random.Random(seed)
    out: list[TrackFeature] = []
    for i in range(n):
        bpm = 118.0 + rng.uniform(-6.0, 10.0)
        key = _CAMELOT_ORDER[i % len(_CAMELOT_ORDER)]
        energy = 1 + (i % 10)
        artist = f"Artist {i % 20}"
        out.append(
            TrackFeature(
                stable_id=f"t-{i:05d}",
                artist=artist,
                bpm=round(bpm, 2),
                key_camelot=key,
                energy=energy,
            )
        )
    return out


@pytest.fixture
def tracks_200() -> list[TrackFeature]:
    return make_tracks(200, seed=7)


@pytest.fixture
def tracks_20() -> list[TrackFeature]:
    return make_tracks(20, seed=11)
