"""Real state.db fixtures for the health-light tests.

Every helper writes through the production state layer (real migrations, the
real ``upsert_location`` probe) and real files under ``tmp_path``: disk truth,
no stubbed stat results.
"""
from __future__ import annotations

import json
import sqlite3
import wave
from pathlib import Path

from apps.shared.state import locations as state_locations
from apps.shared.state.db import open_rw
from apps.stems import artifacts as stem_artifacts

STAMP = "2026-10-01T00:00:00Z"
OTHER_MACHINE = "0" * 31 + "1"


def make_state_db(data_dir: Path) -> Path:
    state_db = data_dir / "state" / "state.db"
    open_rw(state_db).close()
    return state_db


def seed_track(state_db: Path, stable_id: str, file_path: str | None) -> None:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "duration_ms, file_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (stable_id, "inferred", f"t-{stable_id}", '["Artist"]', 200_000, file_path, STAMP, STAMP),
        )
        conn.commit()
    finally:
        conn.close()


def claim_here(state_db: Path, stable_id: str, file_path: Path) -> None:
    """Record ``file_path`` as a location on THIS machine via the real probe."""
    conn = open_rw(state_db)
    try:
        state_locations.upsert_location(
            conn, stable_id=stable_id, kind="local", file_path=str(file_path), role="primary"
        )
    finally:
        conn.close()


def claim_on_other_machine(state_db: Path, stable_id: str, file_path: str) -> None:
    """A synced-in row: another machine saw the file, this one never did."""
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO machines (machine_id, name, platform, is_hub, "
            "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (OTHER_MACHINE, "other-machine", "macos", 0, STAMP, STAMP),
        )
        conn.execute(
            "INSERT INTO track_locations (location_id, stable_id, machine_id, kind, role, "
            "file_path, available, probed_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"loc-{stable_id}", stable_id, OTHER_MACHINE, "local", "primary", file_path, 1,
             STAMP, STAMP, STAMP),
        )
        conn.commit()
    finally:
        conn.close()


def audio_file(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * 4096)
    return path


def write_stem_bundle(root: Path, stable_id: str) -> Path:
    """A real, aligned v3 bundle the production reader accepts."""
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in stem_artifacts.ROFORMER_PARTS:
        with wave.open(str(bundle / f"{part}.wav"), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44_100)
            output.writeframes(b"\x00\x00" * 24)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stable_id": stable_id,
                "layout": "roformer2",
                "model": {"name": "roformer", "version": "1"},
                "source": {"path": "/music/source.wav", "sha256": "a" * 64},
                "audio": {"sample_rate": 44_100, "frame_count": 12, "channels": 2},
                "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            }
        ),
        encoding="utf-8",
    )
    return bundle


def put_in_cloud(s3, cfg, scratch: Path, stable_id: str) -> dict[str, str]:
    """Upload a real bundle's bytes to the in-memory R2 and return its index
    entry ({filename: sha256}), exactly what the push rail journals."""
    import hashlib
    import shutil

    from apps.cloud.asset_store import asset_object_key

    bundle = write_stem_bundle(scratch, stable_id)
    entry: dict[str, str] = {}
    for file_path in sorted(bundle.iterdir()):
        body = file_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[file_path.name] = digest
    shutil.rmtree(bundle)
    return entry


def arm_cloud(app, data_dir: Path, s3, cfg, index: dict[str, dict[str, str]]) -> None:
    """Arm stem hydration on ``app`` the way the engine does: a source on
    app state and the R2 index in its local cache file."""
    from apps.cloud import stem_index
    from apps.cloud.stem_source import DirectR2Source

    app.state.stem_hydration_source = DirectR2Source(cfg, s3)
    app.state.stem_hydration_unarmed_reason = None
    stem_index.save_cached_index(data_dir, index)


def vocal_worker_result() -> dict[str, object]:
    from apps.vocals import cache as vocals_cache

    return {
        "schema": vocals_cache.VOCAL_CACHE_SCHEMA,
        "source": vocals_cache.VOCAL_CACHE_SOURCE,
        "fps": 2.0,
        "duration_s": 120.0,
        "coverage_pct": 41.7,
        "regions": [{"start_s": 10.0, "end_s": 60.0, "confidence": 0.84}],
        "params": {"hop_s": 0.5, "on_ratio": 0.1},
        "device": "cpu",
        "timings": {"load_s": 1.0, "separate_s": 100.0},
        "source_sample_rate": 44100,
        "analysis_sample_rate": 44100,
    }


def write_vocals(vocal_dir: Path, stable_id: str, audio: Path) -> None:
    """A real vocal-cache entry through the production writer."""
    from apps.vocals import cache as vocals_cache

    vocal_dir.mkdir(parents=True, exist_ok=True)
    vocals_cache.write_entry(vocal_dir / f"{stable_id}.json", vocal_worker_result(), audio)


def write_lyrics(lyrics_dir: Path, stable_id: str) -> None:
    """A real lyrics-cache entry through the production writer."""
    from apps.lyrics import cache as lyrics_cache

    lyrics_cache.write(
        lyrics_dir / f"{stable_id}.json",
        lyrics_cache.Lyrics(
            stable_id=stable_id,
            source="lrclib",
            lines=(lyrics_cache.LyricLine(start_ms=0, text="la la"),),
        ),
    )


def write_fetch_verdict(data_dir: Path, stable_id: str, outcome: str) -> None:
    """A real lyrics-fetch verdict through the production writer."""
    from apps.lyrics import fetch_verdicts

    fetch_verdicts.write_verdict(
        data_dir,
        fetch_verdicts.FetchVerdict(
            stable_id=stable_id,
            outcome=outcome,  # type: ignore[arg-type]
            source=None,
            vocals_sha256=None,
            hub_code=None,
            hub_message=None,
            language_iso3=None,
            recorded_at=STAMP,
        ),
    )


def write_analysis_row(state_db: Path, stable_id: str, *, backend: str = "librosa") -> None:
    """A real row in the real ``analysis`` table (schema from the store)."""
    from apps.analysis.store import open_conn

    conn = open_conn(state_db)
    try:
        conn.execute(
            "INSERT INTO analysis (stable_id, backend, backend_version, analyzed_at, "
            "duration_s, sample_rate, bpm, bpm_confidence, key_camelot, key_openkey, "
            "key_confidence, energy, energy_source, record_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stable_id, backend, "test", STAMP, 200.0, 44_100, 120.0, 1.0, "8A", "1m",
             1.0, 5, "test", "{}"),
        )
        conn.commit()
    finally:
        conn.close()
