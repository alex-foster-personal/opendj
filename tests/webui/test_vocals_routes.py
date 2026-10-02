"""apps.webui.server.routes.vocals: HTTP trigger + status (issue #1038).

Hermetic: builds a throwaway state.db + master.plain.db, exactly the
tests/vocals/test_cli_queue.py shape, so the route's classify()-based
refusal path is exercised for real. The route functions are called
directly (they are plain functions, not bound to the ASGI stack), which
keeps the test independent of the full ``create_app()`` machinery that
``resolve_content``/``vocals_for_content`` would otherwise require a real
master.plain.db + path-map fixture to drive end to end.

Regression one-liners:
  - if a track with a stem bundle isn't claimed under from-stems then broken
  - if a claimed from-stems track's vocal-cache entry isn't GET-visible then broken
  - if a track with no local ANLZ .DAT isn't refused missing_analysis then broken
  - if refusing a track still writes a vocal-cache entry then broken
  - if a stable_id with no rekordbox mapping isn't refused unmapped then broken
  - if a todo track with no stem bundle isn't refused missing_stems under from-stems then broken
  - if an already-cached track isn't refused cached_demucs then broken
  - if stable_ids and playlist_id are both given or both omitted then broken
  - if playlist_id doesn't resolve through backend.get_playlist then broken
"""
from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.adapters.rekordbox import config as rb_config
from apps.vocals import cache as vcache
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist
from apps.webui.server.routes import vocals as vocals_routes

pytestmark = [pytest.mark.requirement("PARITY-08"), pytest.mark.rb_parity]

_PVDI_FIXED_HEADER = bytes.fromhex("0000040056220001")


def _empty_2ex() -> bytes:
    head = b"PMAI" + struct.pack(">II", 28, 28)
    return head + b"\x00" * (28 - len(head))


def _pvdi_2ex() -> bytes:
    """Minimal PVDI carrier with one sufficiently long vocal region."""
    envelope = bytes([3]) * 64
    section_length = 24 + len(envelope)
    section = (
        b"PVDI"
        + struct.pack(">II", 24, section_length)
        + _PVDI_FIXED_HEADER
        + struct.pack(">I", len(envelope))
        + envelope
    )
    total = 28 + len(section)
    return b"PMAI" + struct.pack(">II", 28, total) + b"\x00" * 16 + section


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp data root with state.db + master.plain.db + audio/.2EX/stems.

    Tracks (stable_id -> scenario):
      hasstem   audio on disk, ANLZ .DAT present, stem bundle
                -> todo, from-stems succeeds
      nostem    audio on disk, ANLZ .DAT present, NO stem bundle
                -> todo, from-stems refuses missing_stems
      noanlz    audio on disk, no ANLZ .DAT -> missing_analysis
      cached    audio on disk, ANLZ .DAT present, valid cache already
                -> cached_demucs
    """
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()

    def _audio(name: str) -> str:
        p = media / f"{name}.mp3"
        p.write_bytes(b"audio " + name.encode())
        return str(p)

    def _twoex(name: str, payload: bytes | None) -> str | None:
        if payload is None:
            return None
        dat = media / f"{name}.DAT"
        (media / f"{name}.2EX").write_bytes(payload)
        dat.write_bytes(b"dat")
        return str(dat)

    rows = [
        ("hasstem", 100, _audio("hasstem"), _twoex("hasstem", _empty_2ex())),
        ("nostem", 100, _audio("nostem"), _twoex("nostem", _empty_2ex())),
        ("noanlz", 120, _audio("noanlz"), None),
        ("cached", 100, _audio("cached"), _twoex("cached", _empty_2ex())),
        ("pvdi", 100, _audio("pvdi"), _twoex("pvdi", _pvdi_2ex())),
        ("roformer", 100, _audio("roformer"), _twoex("roformer", _empty_2ex())),
    ]

    state = sqlite3.connect(data / "state" / "state.db")
    state.executescript(
        """
        CREATE TABLE tracks (
            stable_id TEXT PRIMARY KEY, title TEXT, deleted_at TEXT);
        CREATE TABLE track_vendor_ids (
            stable_id TEXT, vendor TEXT, vendor_id TEXT, deleted_at TEXT);
        """
    )
    master = sqlite3.connect(data / "master.plain.db")
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, ImagePath TEXT, AnalysisDataPath TEXT, Commnt TEXT, "
        "GenreID TEXT, rb_local_deleted INTEGER)"
    )
    master.execute(
        "CREATE TABLE djmdGenre (ID TEXT, Name TEXT, rb_local_deleted INTEGER)"
    )
    for i, (sid, length, folder, adp) in enumerate(rows):
        vendor_id = f"v{i}"
        state.execute(
            "INSERT INTO tracks (stable_id, title) VALUES (?, ?)", (sid, f"title-{sid}")
        )
        state.execute(
            "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?, NULL)",
            (sid, vendor_id),
        )
        master.execute(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, 0)",
            (vendor_id, f"title-{sid}", length, folder, adp),
        )
    state.execute(
        "INSERT INTO tracks (stable_id, title) VALUES ('local-only', 'local only')"
    )
    state.commit()
    state.close()
    master.commit()
    master.close()

    entry_result = {
        "schema": vcache.VOCAL_CACHE_SCHEMA,
        "source": "demucs-htdemucs",
        "fps": 2.0,
        "duration_s": 90.0,
        "coverage_pct": 40.0,
        "regions": [{"start_s": 0.0, "end_s": 30.0, "confidence": 0.7}],
        "params": {},
    }
    vcache.write_entry(
        vcache.cache_path(data, "cached"), entry_result, media / "cached.mp3"
    )

    monkeypatch.setattr(vocals_routes, "DATA_DIR", data)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data / "master.plain.db")
    monkeypatch.setattr(rb_config, "STATE_DB", data / "state" / "state.db")
    monkeypatch.setattr(rb_config, "VOCAL_CACHE_DIR", vcache.cache_dir(data))
    return data


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    """Mounted production app over the disposable state + Rekordbox fixtures."""
    backend = InMemoryBackend()
    backend.seed_playlist(Playlist(playlist_id="pl1", name="p", items=["hasstem"]))
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(data_dir / "state" / "state.db"),
        mount_frontend=False,
        stem_roots=(data_dir / "configured-stems",),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def stem_bundle(data_dir: Path) -> None:
    import numpy as np
    sf = pytest.importorskip("soundfile", reason="needs the optional soundfile package")

    from apps.stems.artifacts import STEM_PARTS

    root = data_dir / "configured-stems"
    bundle = root / "hasstem"
    bundle.mkdir(parents=True)
    sr = 44100
    n = sr * 4
    t = np.arange(n, dtype=np.float32) / sr
    vocals = np.zeros(n, dtype=np.float32)
    vocals[sr : 3 * sr] = 0.4 * np.sin(2 * np.pi * 440 * t[sr : 3 * sr])
    quiet = 0.02 * np.sin(2 * np.pi * 110 * t)
    for part, mono in (
        ("vocals", vocals), ("drums", quiet), ("bass", quiet), ("other", quiet),
    ):
        sf.write(str(bundle / f"{part}.wav"), mono, sr, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": "hasstem",
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/tmp/source.wav", "sha256": "a" * 64},
        "files": {p: f"{p}.wav" for p in STEM_PARTS},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def roformer_bundle(data_dir: Path) -> None:
    import numpy as np
    sf = pytest.importorskip("soundfile", reason="needs the optional soundfile package")

    bundle = data_dir / "configured-stems" / "roformer"
    bundle.mkdir(parents=True)
    sr = 44100
    samples = sr * 4
    t = np.arange(samples, dtype=np.float32) / sr
    vocals = 0.4 * np.sin(2 * np.pi * 440 * t)
    instrumental = 0.02 * np.sin(2 * np.pi * 110 * t)
    sf.write(str(bundle / "vocals.wav"), vocals, sr, subtype="PCM_16")
    sf.write(str(bundle / "instrumental.wav"), instrumental, sr, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": "roformer",
        "layout": "roformer2",
        "model": {"name": "mel-band-roformer", "version": "1.0.0"},
        "source": {"path": "/tmp/source.wav", "sha256": "b" * 64},
        "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


# ----- POST /vocals/analyze --------------------------------------------------


def test_from_stems_happy_path_claims_and_writes_cache(
    client: TestClient, data_dir: Path, stem_bundle: None
) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": ["hasstem"], "refused": {}}
    loaded = vcache.load_valid_entry(
        vcache.cache_path(data_dir, "hasstem"), data_dir.parent / "media" / "hasstem.mp3"
    )
    assert loaded is not None
    assert loaded["regions"]


def test_missing_analysis_refuses_and_writes_nothing(client: TestClient, data_dir: Path) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["noanlz"], "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": [], "refused": {"noanlz": "missing_analysis"}}
    assert not vcache.cache_path(data_dir, "noanlz").exists()


def test_unmapped_stable_id_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["ghost"], "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": [], "refused": {"ghost": "unmapped"}}


def test_todo_track_with_no_stem_bundle_refuses_missing_stems(client: TestClient) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["nostem"], "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": [], "refused": {"nostem": "missing_stems"}}


def test_already_cached_track_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["cached"], "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": [], "refused": {"cached": "cached_demucs"}}


def test_playlist_id_resolves_stable_ids_through_backend(
    client: TestClient, stem_bundle: None
) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"playlist_id": "pl1", "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": ["hasstem"], "refused": {}}


def test_duplicate_stable_ids_are_processed_once(client: TestClient, stem_bundle: None) -> None:
    response = client.post(
        "/api/v1/vocals/analyze",
        json={"stable_ids": ["hasstem", "hasstem"], "mode": "from-stems"},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": ["hasstem"], "refused": {}}


def test_ready_roformer_bundle_is_derived(client: TestClient, roformer_bundle: None) -> None:
    response = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["roformer"], "mode": "from-stems"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": ["roformer"], "refused": {}}


def test_exactly_one_of_stable_ids_or_playlist_id_required() -> None:
    with pytest.raises(ValidationError):
        vocals_routes.VocalsAnalyzeIn(mode="one")
    with pytest.raises(ValidationError):
        vocals_routes.VocalsAnalyzeIn(
            stable_ids=["a"], playlist_id="pl1", mode="one"
        )


# ----- GET /vocals/{stable_id}/status -----------------------------------------


def test_status_not_analyzed_when_unmapped(client: TestClient) -> None:
    response = client.get("/api/v1/vocals/local-only/status")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "stable_id": "local-only", "status": "not_analyzed", "fps": None, "regions": []
    }


def test_status_reports_demucs_after_from_stems_write(
    client: TestClient, stem_bundle: None
) -> None:
    analyze = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
    )
    assert analyze.status_code == 202, analyze.text
    response = client.get("/api/v1/vocals/hasstem/status")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "demucs"
    assert response.json()["regions"]


def test_status_reports_rekordbox_regions_without_demucs_confidence(client: TestClient) -> None:
    response = client.get("/api/v1/vocals/pvdi/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rekordbox"
    assert body["regions"]
    assert body["regions"][0]["confidence"] is None
