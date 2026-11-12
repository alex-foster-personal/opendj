"""Shared state.db pytest fixture and synthetic gridded-track writer (issue #10406).

Production paths only: ``open_conn`` for schema, ``StateWriter.upsert_track``
for the track row, ``record_from_payload`` + ``upsert_record`` for the own
beatgrid record.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_beatgrid.flags import ACTIVATION_PEAK_THRESHOLD, MIN_BEATS
from apps.analysis_beatgrid.version import PRODUCER_VERSION
from apps.shared.state import db as shared_state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

# Track rows use FIXTURE_PROVENANCE_NAME as the StateWriter actor; analysis
# upserts stamp events with actor ``apps.analysis`` (store.publish). Fixture
# lineage on the own beatgrid record is carried only through model_sha256.
FIXTURE_PROVENANCE_NAME = "fixture-synthetic"
FIXTURE_MODEL_SHA256 = hashlib.sha256(FIXTURE_PROVENANCE_NAME.encode()).hexdigest()
SYNTHETIC_DECODE_FINGERPRINT = "cc" * 32
SYNTHETIC_ACTIVATIONS_NPZ = "/fixtures/synthetic.activations.npz"
_RUNNER_FPS = 50.0


@dataclass(frozen=True)
class PhraseMarker:
    start_ms: float
    bars: int


@dataclass(frozen=True)
class SyntheticGriddedTrack:
    stable_id: str
    beats_ms: tuple[float, ...]
    downbeats_ms: tuple[float, ...]
    phrases: tuple[PhraseMarker, ...]
    record: AnalysisRecord


def create_state_db(tmp_path: Path) -> Path:
    """Open a throwaway state DB through the same ``open_conn`` path the app uses."""
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    return db_path


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """A schema-complete empty state DB under ``tmp_path``, via ``open_conn``."""
    return create_state_db(tmp_path)


def _beat_times_s(
    *,
    bpm: float,
    seconds: float,
    bar1_ms: float,
) -> tuple[list[float], list[float], tuple[float, ...], tuple[float, ...]]:
    period_s = 60.0 / bpm
    bar1_s = bar1_ms / 1000.0
    n_beats = max(MIN_BEATS, int((seconds - bar1_s) / period_s) + 1)
    beats_s = [round(bar1_s + index * period_s, 6) for index in range(n_beats)]
    downbeats_s = beats_s[0::4]
    beats_ms = tuple(round(t * 1000.0, 6) for t in beats_s)
    downbeats_ms = tuple(round(t * 1000.0, 6) for t in downbeats_s)
    return beats_s, downbeats_s, beats_ms, downbeats_ms


def _phrase_markers(
    *,
    downbeats_ms: tuple[float, ...],
    phrase_bars: int,
    last_beat_ms: float,
) -> tuple[PhraseMarker, ...]:
    if phrase_bars <= 0:
        return ()
    markers: list[PhraseMarker] = []
    for index in range(0, len(downbeats_ms), phrase_bars):
        start_ms = downbeats_ms[index]
        if index + phrase_bars < len(downbeats_ms):
            end_ms = downbeats_ms[index + phrase_bars]
        else:
            end_ms = last_beat_ms
        bars = phrase_bars if index + phrase_bars < len(downbeats_ms) else max(1, len(downbeats_ms) - index)
        if end_ms > start_ms:
            markers.append(PhraseMarker(start_ms=start_ms, bars=bars))
    return tuple(markers)


def _runner_payload(
    backend_name: str,
    audio_path: Path,
    *,
    beats_s: list[float],
    downbeats_s: list[float],
    n_frames: int,
) -> dict[str, object]:
    return {
        "schema": 2,
        "producer": backend_name,
        "producer_version": PRODUCER_VERSION,
        "model_sha256": FIXTURE_MODEL_SHA256,
        "threshold": ACTIVATION_PEAK_THRESHOLD,
        "fps": _RUNNER_FPS,
        "results": {
            str(audio_path): {
                "audio": str(audio_path),
                "beats": beats_s,
                "downbeats": downbeats_s,
                "activation_peak": 0.99,
                "n_frames": n_frames,
                "activations_npz": SYNTHETIC_ACTIVATIONS_NPZ,
                "fps": _RUNNER_FPS,
                "decode_fingerprint": SYNTHETIC_DECODE_FINGERPRINT,
                "sample_rate": 44_100,
                "error": None,
            }
        },
    }


def write_synthetic_gridded_track(
    db_path: Path,
    *,
    stable_id: str,
    bpm: float,
    seconds: float,
    bar1_ms: float = 0.0,
    phrase_bars: int = 8,
    title: str | None = None,
) -> SyntheticGriddedTrack:
    """Seed a track and own beatgrid record with a known grid through production writers."""
    # Imported here, not at module top: tests/conftest.py imports this module for ``state_db``, and
    # own_beatgrid pulls numpy, so a top-level import broke every pytest run without the analysis
    # stack (the frontend job's isolated escape-hatch check runs pytest with nothing but pytest).
    from apps.analysis.backends.own_beatgrid import BACKEND_NAME, record_from_payload

    beats_s, downbeats_s, beats_ms, downbeats_ms = _beat_times_s(bpm=bpm, seconds=seconds, bar1_ms=bar1_ms)
    last_beat_ms = beats_ms[-1] if beats_ms else bar1_ms
    phrases = _phrase_markers(downbeats_ms=downbeats_ms, phrase_bars=phrase_bars, last_beat_ms=last_beat_ms)
    audio_path = Path(f"/fixtures/synthetic/{stable_id}.wav")
    duration_ms = int(seconds * 1000)
    track_title = title if title is not None else f"fixture-{stable_id[:8]}"

    conn = shared_state_db.open_rw(db_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor=FIXTURE_PROVENANCE_NAME)
    try:
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="inferred",
            title=track_title,
            artists=[],
            album=None,
            isrc=None,
            duration_ms=duration_ms,
            file_path=str(audio_path),
        )
    finally:
        writer.close()
        conn.close()

    n_frames = int(max(beats_s) * _RUNNER_FPS) + 1
    payload = _runner_payload(BACKEND_NAME, audio_path, beats_s=beats_s, downbeats_s=downbeats_s, n_frames=n_frames)
    record = record_from_payload(
        payload,
        stable_id=stable_id,
        audio_path=audio_path,
        model_sha256=FIXTURE_MODEL_SHA256,
        decode_fingerprint=SYNTHETIC_DECODE_FINGERPRINT,
    )
    upsert_record(record, db_path=db_path)
    return SyntheticGriddedTrack(
        stable_id=stable_id,
        beats_ms=beats_ms,
        downbeats_ms=downbeats_ms,
        phrases=phrases,
        record=record,
    )


def assert_state_db_schema(path: Path) -> None:
    """Fail closed when core Phase 5 tables are absent."""
    conn = sqlite3.connect(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "tracks" in tables, f"missing tracks table; saw {sorted(tables)}"
        assert "analysis" in tables, f"missing analysis table; saw {sorted(tables)}"
    finally:
        conn.close()
