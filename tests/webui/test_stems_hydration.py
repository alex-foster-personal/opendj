"""On-demand R2 hydration wired into the stems routes (ADR-0020).

* [if] a bundle is missing locally but present in the R2 index [then] the
  manifest GET returns immediately (never blocking on the 4-part fetch) with
  ``hydrating: true``, and the part GET waits (bounded) and then serves the
  real bytes.
* [if] R2 credentials/config are not bound on ``app.state`` [then] both
  routes behave exactly as before this feature (unchanged 200
  unavailable / 404).
* [if] the index says a bundle exists but hydration cannot produce it
  [then] BOTH routes fail LOUD (HTTP 502, a distinct code), never the
  ordinary 200/404 "no bundle" shape -- the storage/stems view must be able
  to tell "nothing here" from "something is wrong" (fail-loud requirement).
"""
from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apps.webui.server.routes.stems as stems_module
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.lock import FakeS3Client as _UnusedFakeS3  # noqa: F401 (documents the sibling fake)
from apps.cloud.stem_index import save_cached_index
from apps.webui.server.routes.stems import router


def _wav_bytes(*, frames: int = 8, sample_rate: int = 44_100, channels: int = 2) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(b"\x00\x00" * frames * channels)
    return buf.getvalue()


def _manifest_bytes(stable_id: str) -> bytes:
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
    return (json.dumps(manifest) + "\n").encode("utf-8")


class _InMemoryAssetS3:
    """Minimal AssetS3Client fake, local to this test module (routes tests
    should not import the cloudsync test tree's fixtures)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}

    def head_object(self, bucket, key):
        from apps.cloud.asset_store import AssetHead

        body = self.store.get((bucket, key))
        return None if body is None else AssetHead(size=len(body), etag="x")

    def get_object(self, bucket, key):
        body = self.store.get((bucket, key))
        return None if body is None else (body, "x")

    def put_object_if_none_match(self, bucket, key, body):
        if not isinstance(body, bytes):
            body = body.read()
        if (bucket, key) in self.store:
            return False, None
        self.store[(bucket, key)] = body
        return True, "x"

    def delete_object(self, bucket, key):
        return self.store.pop((bucket, key), None) is not None


def _cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct", r2_access_key_id="id", r2_secret_access_key="secret",
        state_bucket="test-state", audio_bucket="test-audio",
        hostname="host", bind_host="127.0.0.1",
    )


def _seed_bundle(s3: _InMemoryAssetS3, cfg: CloudConfig, stable_id: str) -> dict[str, str]:
    files = {
        "manifest.json": _manifest_bytes(stable_id),
        "vocals.wav": _wav_bytes(), "drums.wav": _wav_bytes(),
        "bass.wav": _wav_bytes(), "other.wav": _wav_bytes(),
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[filename] = digest
    return entry


def _client(
    stems_dir: Path,
    *,
    data_dir: Path,
    hydration_cfg: CloudConfig | None = None,
    hydration_s3: _InMemoryAssetS3 | None = None,
) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_cfg = hydration_cfg
    app.state.stem_hydration_s3 = hydration_s3
    app.state.stem_hydration_data_dir = data_dir
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_hydration_module_state():
    """The route module keeps process-wide in-flight/error registries; clear
    them around every test so tests cannot leak into each other."""
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()
    yield
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()


@pytest.mark.requirement("STEM-15")
def test_manifest_route_enqueues_and_returns_immediately(tmp_path: Path):
    """D2: the manifest GET must never block on the part fetch."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "remote-track")
    save_cached_index(data_dir, {"remote-track": entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/remote-track/stems")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["hydrating"] is True


@pytest.mark.requirement("STEM-15")
def test_part_route_waits_then_serves_hydrated_bytes(tmp_path: Path):
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "remote-track")
    save_cached_index(data_dir, {"remote-track": entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/remote-track/stems/vocals")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert len(resp.content) > 0


@pytest.mark.requirement("STEM-15")
def test_no_hydration_deps_falls_back_to_unchanged_behavior(tmp_path: Path):
    """A machine with no CloudConfig/S3 bound (local mode, no creds) behaves
    exactly as it did before this feature."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    with _client(stems_dir, data_dir=data_dir) as client:
        manifest = client.get("/api/v1/tracks/absent/stems")
        part = client.get("/api/v1/tracks/absent/stems/vocals")
    assert manifest.status_code == 200
    assert manifest.json()["status"] == "unavailable"
    assert manifest.json()["hydrating"] is False
    assert part.status_code == 404


@pytest.mark.requirement("STEM-15")
def test_not_in_index_is_ordinary_unavailable_not_hydrating(tmp_path: Path):
    """No bundle anywhere (not local, not in R2 index) is a settled, cheap
    'unavailable' -- not confused with 'hydrating'."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    save_cached_index(data_dir, {})  # empty index cached

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/truly-nowhere/stems")
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["hydrating"] is False


# --- fail-loud: expected-but-unhydratable must never read as empty ----------


@pytest.mark.requirement("STEM-16")
def test_manifest_route_fails_loud_when_hydration_cannot_produce_the_bundle(tmp_path: Path):
    """The index names a bundle whose part hash is wrong (never actually in
    R2) -- hydration fails, and the NEXT manifest GET must surface that as an
    explicit error, never silently fold back into the ordinary 'unavailable'
    empty state.

    MUTATION TARGET: if the manifest route's recorded-error check is removed
    (falling through to `_enqueue_hydration` / `_unavailable_out` on every
    call instead of consulting `_LAST_HYDRATE_ERROR`), this test goes red
    because the second poll would see hydrating=True forever instead of a
    502.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "broken-track")
    entry["vocals.wav"] = "f" * 64  # never actually pushed
    save_cached_index(data_dir, {"broken-track": entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        first = client.get("/api/v1/tracks/broken-track/stems")
        assert first.status_code == 200
        assert first.json()["hydrating"] is True

        # Hydration ran synchronously enough (small thread pool, tiny fixture)
        # that by the time we poll again the background job has finished and
        # recorded its failure.
        import time

        deadline = time.time() + 5
        second = None
        while time.time() < deadline:
            second = client.get("/api/v1/tracks/broken-track/stems")
            if second.status_code != 200:
                break
            time.sleep(0.02)
        assert second is not None
        assert second.status_code == 502
        assert second.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"


@pytest.mark.requirement("STEM-16")
def test_part_route_fails_loud_on_hydration_error(tmp_path: Path):
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "broken-track")
    entry["vocals.wav"] = "f" * 64  # never actually pushed
    save_cached_index(data_dir, {"broken-track": entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/broken-track/stems/vocals")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"


# --- deck-open/close parity endpoints ----------------------------------------


@pytest.mark.requirement("STEM-14")
def test_deck_open_close_endpoints_update_the_registry(tmp_path: Path):
    from apps.cloud.stem_hydration import OPEN_DECKS

    with _client(tmp_path / "stems", data_dir=tmp_path / "data") as client:
        client.post("/api/v1/tracks/deck-track/stems/deck-open")
        assert OPEN_DECKS.is_open("deck-track")
        client.post("/api/v1/tracks/deck-track/stems/deck-close")
        assert not OPEN_DECKS.is_open("deck-track")
