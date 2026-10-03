"""TrackOut optional-resource flags must predict absent lyrics, auto-cues, stems, artwork."""

from __future__ import annotations

import io
import shutil
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.cloud.stem_index import save_cached_index
from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.cloudsync.conftest import InMemoryAssetS3


@pytest.fixture
def flags_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.close()
    # GET /artwork reads rb_config.STATE_DB, not the TestClient backend.
    # Without this, a missing default STATE_DB 500s STATE_DB_UNAVAILABLE.
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def hydration_flags_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Like flags_client but with stem_hydration_data_dir wired on app.state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.close()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    app.state.stem_hydration_data_dir = data_dir
    with TestClient(app) as client:
        yield client


def _insert_track(client: TestClient, tmp_path: Path, stable_id: str) -> None:
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (stable_id, "inferred", "Test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()


def test_track_out_optional_resource_flags_on_empty_track(
    flags_client: TestClient, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("empty-track", "inferred", "Empty", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    response = flags_client.get("/api/v1/tracks/empty-track")

    assert response.status_code == 200
    body = response.json()
    assert body["lyrics_available"] is False
    assert body["auto_cues_available"] is False
    assert body["stems_available"] is False
    assert body["artwork_available"] is False

    lyrics_response = flags_client.get("/api/v1/tracks/empty-track/lyrics")
    assert lyrics_response.status_code == 404


def test_track_out_lyrics_available_when_cache_file_exists(
    flags_client: TestClient, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "track-with-lyrics",
            "inferred",
            "Lyrics",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    write(
        cache_path(tmp_path, "track-with-lyrics"),
        Lyrics(
            stable_id="track-with-lyrics",
            source="lrclib",
            lines=(LyricLine(start_ms=0, text="Line one"),),
        ),
    )

    response = flags_client.get("/api/v1/tracks/track-with-lyrics")

    assert response.status_code == 200
    assert response.json()["lyrics_available"] is True


def test_track_out_lyrics_available_for_asr_cache(
    flags_client: TestClient, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "track-with-asr-lyrics",
            "inferred",
            "ASR Lyrics",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    write(
        cache_path(tmp_path, "track-with-asr-lyrics"),
        Lyrics(
            stable_id="track-with-asr-lyrics",
            source="asr",
            lines=(LyricLine(start_ms=500, text="hello world"),),
        ),
    )

    response = flags_client.get("/api/v1/tracks/track-with-asr-lyrics")

    assert response.status_code == 200
    assert response.json()["lyrics_available"] is True


def test_track_out_artwork_available_false_for_audio_without_picture(
    flags_client: TestClient, tmp_path: Path
) -> None:
    fixture = (
        Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup" / "src-320.mp3"
    )
    audio_path = tmp_path / "no-art.mp3"
    shutil.copy2(fixture, audio_path)
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "no-art-track",
            "inferred",
            "No Art",
            str(audio_path),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    response = flags_client.get("/api/v1/tracks/no-art-track")

    assert response.status_code == 200
    assert response.json()["artwork_available"] is False

    artwork = flags_client.get("/api/v1/tracks/no-art-track/artwork")
    assert artwork.status_code == 404


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44_100)
        output.writeframes(b"\x00\x00" * 200)


def test_track_out_artwork_available_agrees_with_artwork_route(
    flags_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav_path = tmp_path / "no-art.wav"
    _write_wav(wav_path)
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    connection = state_db.open_rw(state_db_path)
    stable_id = "a" * 40
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            stable_id,
            "inferred",
            "No Art",
            str(wav_path),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    track_resp = flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert track_resp.status_code == 200
    artwork_available = track_resp.json()["artwork_available"]
    assert artwork_available is not True

    artwork_resp = flags_client.get(f"/api/v1/tracks/{stable_id}/artwork")
    if artwork_available is True:
        assert artwork_resp.status_code == 200
    elif artwork_available is False:
        assert artwork_resp.status_code == 404
    elif artwork_available is None:
        assert artwork_resp.status_code == 503
        detail = artwork_resp.json()["detail"]
        assert detail["code"] == "ARTWORK_READER_UNAVAILABLE"


@pytest.mark.requirement("STEM-25")
def test_stems_available_true_when_indexed_but_not_local(
    hydration_flags_client: TestClient, tmp_path: Path
) -> None:
    """A track with no local bundle but a cached R2 index entry reports
    stems_available=True so the frontend's probe issues GET /stems.

    [if] a track has no bundle but a cached index entry [then] stems_available is True, [else stop].
    """
    from tests.webui.test_stems_hydration import _cfg, _seed_bundle

    stable_id = "indexed-not-local"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, stable_id)
    _insert_track(hydration_flags_client, tmp_path, stable_id)
    save_cached_index(data_dir, {stable_id: entry})
    hydration_flags_client.app.state.stem_hydration_cfg = cfg
    hydration_flags_client.app.state.stem_hydration_s3 = s3
    hydration_flags_client.app.state.stems_dir = tmp_path / "stems"

    track_resp = hydration_flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert track_resp.status_code == 200
    assert track_resp.json()["stems_available"] is True

    stems_resp = hydration_flags_client.get(f"/api/v1/tracks/{stable_id}/stems")
    assert stems_resp.status_code == 200
    assert stems_resp.json().get("hydrating") is True


@pytest.mark.requirement("STEM-25")
def test_stems_available_false_when_hydration_disabled(
    flags_client: TestClient, tmp_path: Path
) -> None:
    """[if] a track is indexed, hydration unbound [then] stems_available is False, [else stop]."""
    stable_id = "indexed-no-hydration"
    data_dir = tmp_path / "data"
    _insert_track(flags_client, tmp_path, stable_id)
    save_cached_index(
        data_dir,
        {stable_id: {"manifest.json": "a" * 64, "vocals.wav": "b" * 64}},
    )

    response = flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert response.status_code == 200
    assert response.json()["stems_available"] is False


@pytest.mark.requirement("STEM-25")
def test_stems_available_false_when_not_in_index(
    hydration_flags_client: TestClient, tmp_path: Path
) -> None:
    """[if] a track has no bundle, no index entry [then] stems_available is False, [else stop]."""
    stable_id = "not-in-index"
    data_dir = tmp_path / "data"
    _insert_track(hydration_flags_client, tmp_path, stable_id)
    save_cached_index(data_dir, {"other-track": {"manifest.json": "a" * 64}})

    response = hydration_flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert response.status_code == 200
    assert response.json()["stems_available"] is False


@pytest.mark.requirement("STEM-25")
def test_stems_available_true_from_local_summary_without_hydration(
    flags_client: TestClient, tmp_path: Path
) -> None:
    """Local bundle path is unaffected when hydration is not wired.

    [if] a local bundle exists, hydration unwired [then] stems_available still True, [else stop].
    """
    import json

    stable_id = "local-bundle-track"
    stems_dir = tmp_path / "stems"
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {
            "vocals": "vocals.wav", "drums": "drums.wav",
            "bass": "bass.wav", "other": "other.wav",
        },
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    for part in ("vocals", "drums", "bass", "other"):
        _write_wav(bundle_dir / f"{part}.wav")

    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (stable_id, "inferred", "Local", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    flags_client.app.state.stems_dir = stems_dir
    response = flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert response.status_code == 200
    assert response.json()["stems_available"] is True


def _minimal_jpeg_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color="red").save(buf, format="JPEG")
    data = buf.getvalue()
    assert data[:2] == b"\xff\xd8"
    return data


def _listing_row_artwork(
    client: TestClient, stable_id: str
) -> bool | None:
    list_resp = client.get("/api/v1/tracks")
    assert list_resp.status_code == 200
    for item in list_resp.json()["items"]:
        if item["stable_id"] == stable_id:
            return item["artwork_available"]
    raise AssertionError(f"stable_id {stable_id} missing from listing")


@pytest.mark.requirement("PARITY-04")
def test_track_detail_artwork_available_agrees_with_listing_unmapped(
    flags_client: TestClient, tmp_path: Path
) -> None:
    """[if] unmapped embedded art [then] list and detail agree on artwork_available, [else stop]."""
    from tests.support.embed_picture import with_id3_apic

    fixture = (
        Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup" / "src-320.mp3"
    )
    jpeg_bytes = _minimal_jpeg_bytes()
    audio_path = tmp_path / "embedded-art.mp3"
    audio_path.write_bytes(with_id3_apic(fixture.read_bytes(), jpeg_bytes))

    stable_id = "c" * 40
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            stable_id,
            "inferred",
            "Embedded Art",
            str(audio_path),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    listing_art = _listing_row_artwork(flags_client, stable_id)
    detail_resp = flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert detail_resp.status_code == 200
    detail_art = detail_resp.json()["artwork_available"]

    assert listing_art is True
    assert detail_art is True
    assert listing_art == detail_art


@pytest.mark.requirement("PARITY-04")
def test_track_detail_artwork_available_agrees_with_listing_unmapped_without_picture(
    flags_client: TestClient, tmp_path: Path
) -> None:
    """[if] a local file has no picture [then] list and detail agree on False, [else stop].

    The reader is tinytag, a core dependency (issue #4717), so this is a checked
    False everywhere now, never the old "could not check" None.
    """

    wav_path = tmp_path / "local.wav"
    _write_wav(wav_path)
    stable_id = "b" * 40
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            stable_id,
            "inferred",
            "Local",
            str(wav_path),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    listing_art = _listing_row_artwork(flags_client, stable_id)
    detail_resp = flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert detail_resp.status_code == 200
    detail_art = detail_resp.json()["artwork_available"]

    assert listing_art is False
    assert detail_art is False
    assert listing_art == detail_art
