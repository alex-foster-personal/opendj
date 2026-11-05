"""Request-scoped own-vocals and phrase readers.

[if] two apps share an id [then] each serves only its own library data, [else stop].

Regression one-liners:
  - if status or /anlz reads another app's same-id vocal cache then broken
  - if failed-attempt status reads the process-default data root then broken
  - if /anlz reads another app's same-id structure sidecar then broken
  - if a flat state database is inferred as state/state.db then broken
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis_structure import pipeline as structure_pipeline
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.stems.artifacts import STEM_PARTS
from apps.vocals import attempts as vattempts
from apps.vocals import cache as vcache
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

STABLE_ID = "same-track"
FAILED_ID = "failed-track"
QUEUE_ID = "queue-track"


@dataclass(frozen=True)
class Library:
    data_dir: Path
    state_db: Path
    fps: float
    start_s: float
    phrase_label: str


def _write_library(
    root: Path,
    *,
    fps: float,
    start_s: float,
    phrase_label: str,
    flat_state_db: bool = False,
) -> Library:
    data_dir = root / "data"
    state_path = data_dir / ("library.db" if flat_state_db else "state/state.db")
    audio_dir = root / "media"
    audio_dir.mkdir(parents=True)
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="unit-test")
    for stable_id in (STABLE_ID, FAILED_ID, QUEUE_ID):
        audio = audio_dir / f"{stable_id}.mp3"
        audio.write_bytes(f"audio-{phrase_label}-{stable_id}".encode())
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="inferred",
            title=stable_id,
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=8_000,
            file_path=str(audio),
        )
    conn.commit()
    conn.close()

    vcache.write_entry(
        vcache.cache_path(data_dir, STABLE_ID),
        {
            "schema": vcache.VOCAL_CACHE_SCHEMA,
            "source": "demucs-htdemucs",
            "fps": fps,
            "duration_s": 8.0,
            "coverage_pct": 25.0,
            "regions": [
                {
                    "start_s": start_s,
                    "end_s": start_s + 2.0,
                    "confidence": 0.75,
                }
            ],
            "params": {"hop_s": 1.0 / fps},
        },
        audio_dir / f"{STABLE_ID}.mp3",
    )
    vattempts.record_failure(data_dir, FAILED_ID, RuntimeError(f"failure-{phrase_label}"))
    sidecar = structure_pipeline.build_sidecar(
        STABLE_ID,
        {
            "status": "ok",
            "segments": [
                {"start": 0.0, "end": 4.0, "label": phrase_label},
                {"start": 4.0, "end": 8.0, "label": f"{phrase_label}-out"},
            ],
            "versions": {"allin1": "test"},
        },
        [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
        {"backend": "test", "backend_version": "1"},
    )
    structure_pipeline.write_sidecar(data_dir, sidecar)
    return Library(data_dir, state_path, fps, start_s, phrase_label)


def _client(
    library: Library, *, stem_roots: tuple[Path, ...] = ()
) -> TestClient:
    backend = SqliteBackend(library.state_db)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(library.state_db),
        mount_frontend=False,
        stem_roots=stem_roots,
    )
    return TestClient(app)


def _status(client: TestClient, stable_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/vocals/{stable_id}/status")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _anlz(client: TestClient, stable_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/tracks/{stable_id}/anlz")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _write_stem_bundle(library: Library, stable_id: str) -> Path:
    import numpy as np

    sf = pytest.importorskip("soundfile", reason="needs the optional soundfile package")
    bundle = library.data_dir / "configured-stems" / stable_id
    bundle.mkdir(parents=True)
    sample_rate = 44_100
    frame_count = sample_rate * 4
    time = np.arange(frame_count, dtype=np.float32) / sample_rate
    vocals = np.zeros(frame_count, dtype=np.float32)
    vocals[sample_rate : 3 * sample_rate] = 0.4 * np.sin(
        2 * np.pi * 440 * time[sample_rate : 3 * sample_rate]
    )
    quiet = 0.02 * np.sin(2 * np.pi * 110 * time)
    for part, mono in (
        ("vocals", vocals),
        ("drums", quiet),
        ("bass", quiet),
        ("other", quiet),
    ):
        sf.write(str(bundle / f"{part}.wav"), mono, sample_rate, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": str(library.data_dir), "sha256": "a" * 64},
        "files": {part: f"{part}.wav" for part in STEM_PARTS},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle.parent


@pytest.fixture
def libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Library, Library, Library]:
    library_a = _write_library(
        tmp_path / "a", fps=2.0, start_s=1.0, phrase_label="library-a"
    )
    library_b = _write_library(
        tmp_path / "b", fps=4.0, start_s=3.0, phrase_label="library-b"
    )
    process_default = _write_library(
        tmp_path / "process", fps=8.0, start_s=5.0, phrase_label="process-default"
    )
    monkeypatch.setattr(rb_config, "DATA_DIR", process_default.data_dir)
    monkeypatch.setattr(rb_config, "STATE_DB", process_default.state_db)
    monkeypatch.setattr(
        rb_config, "MASTER_PLAIN_DB", process_default.data_dir / "master.plain.db"
    )
    monkeypatch.setattr(
        rb_config, "VOCAL_CACHE_DIR", vcache.cache_dir(process_default.data_dir)
    )
    monkeypatch.setattr(
        rb_config,
        "LOCAL_WAVEFORM_CACHE_DIR",
        process_default.data_dir / "state" / "local-waveform-cache",
    )
    return library_a, library_b, process_default


@pytest.mark.requirement("VOCALS-04")
def test_two_apps_serve_their_own_vocals(
    libraries: tuple[Library, Library, Library],
) -> None:
    """[if] two apps share an id [then] status and /anlz serve each app's vocals, [else stop]."""
    library_a, library_b, process_default = libraries
    for library in (library_a, library_b):
        client = _client(library)
        status_vocals = _status(client, STABLE_ID)
        anlz_vocals = _anlz(client, STABLE_ID)["vocals"]
        assert status_vocals["status"] == anlz_vocals["status"] == "demucs"
        assert status_vocals["fps"] == anlz_vocals["fps"] == library.fps
        assert status_vocals["regions"] == anlz_vocals["regions"]
        assert status_vocals["regions"][0]["start_s"] == library.start_s
        assert status_vocals["fps"] != process_default.fps


@pytest.mark.requirement("VOCALS-03")
def test_two_apps_serve_their_own_failed_attempt(
    libraries: tuple[Library, Library, Library],
) -> None:
    """[if] same-id attempts differ [then] status serves the selected library, [else stop]."""
    library_a, library_b, _process_default = libraries
    for library in (library_a, library_b):
        client = _client(library)
        assert _status(client, FAILED_ID)["failed_attempt"]["reason"] == (
            f"failure-{library.phrase_label}"
        )


@pytest.mark.requirement("VOCALS-03")
def test_from_stems_writes_and_reads_only_the_selected_library(
    libraries: tuple[Library, Library, Library],
) -> None:
    """[if] from-stems targets one app [then] its output and readback stay there, [else stop]."""
    library_a, library_b, process_default = libraries
    stem_root = _write_stem_bundle(library_a, QUEUE_ID)
    client = _client(library_a, stem_roots=(stem_root,))
    response = client.post(
        "/api/v1/vocals/analyze",
        json={"stable_ids": [QUEUE_ID], "mode": "from-stems"},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": [QUEUE_ID], "refused": {}}
    status = _status(client, QUEUE_ID)

    selected_entry = vcache.load_valid_entry(
        vcache.cache_path(library_a.data_dir, QUEUE_ID),
        library_a.data_dir.parent / "media" / f"{QUEUE_ID}.mp3",
    )
    assert selected_entry is not None
    assert selected_entry["regions"]
    assert status["status"] == "demucs"
    assert status["regions"]
    for untouched in (library_b, process_default):
        output = vcache.cache_path(untouched.data_dir, QUEUE_ID)
        assert not output.exists()
        assert not output.with_suffix(".json.lock").exists()


@pytest.mark.requirement("STRUCT-02")
def test_two_apps_serve_their_own_phrase_sidecar(
    libraries: tuple[Library, Library, Library],
) -> None:
    """[if] same-id sidecars differ [then] /anlz serves the selected library, [else stop]."""
    library_a, library_b, _process_default = libraries
    for library in (library_a, library_b):
        client = _client(library)
        payload = _anlz(client, STABLE_ID)
        assert payload["phrases_source"] == "own"
        assert payload["phrases"][0]["label"] == library.phrase_label


@pytest.mark.requirement("VOCALS-03")
def test_flat_state_database_uses_the_exact_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] state.db is flat [then] status opens that exact database, [else stop]."""
    library = _write_library(
        tmp_path / "flat",
        fps=3.0,
        start_s=2.0,
        phrase_label="flat-library",
        flat_state_db=True,
    )
    monkeypatch.setattr(rb_config, "STATE_DB", library.state_db)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", library.data_dir / "master.plain.db")
    client = _client(library)
    response = _status(client, STABLE_ID)
    assert response["status"] == "demucs"
    assert response["fps"] == library.fps
