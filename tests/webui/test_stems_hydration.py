"""On-demand R2 hydration wired into the stems routes (ADR-0024).

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
from apps.cloud import stem_index
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_index import (
    INDEX_OBJECT_KEY,
    local_index_cache_path,
    publish_index,
    save_cached_index,
)
from apps.cloud.stem_source import DirectR2Source
from apps.webui.server.routes.stems import router
from apps.webui.server.routes.stems_assets import router as stems_assets_router
from tests.cloudsync.conftest import InMemoryAssetS3


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


def _cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct", r2_access_key_id="id", r2_secret_access_key="secret",
        state_bucket="test-state", audio_bucket="test-audio",
        hostname="host", bind_host="127.0.0.1",
    )


def _seed_bundle(s3: InMemoryAssetS3, cfg: CloudConfig, stable_id: str) -> dict[str, str]:
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
    hydration_s3: InMemoryAssetS3 | None = None,
) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    if hydration_cfg is not None and hydration_s3 is not None:
        app.state.stem_hydration_source = DirectR2Source(
            cfg=hydration_cfg, s3=hydration_s3
        )
        app.state.stem_hydration_cfg = hydration_cfg
        app.state.stem_hydration_s3 = hydration_s3
    else:
        app.state.stem_hydration_source = None
        app.state.stem_hydration_cfg = None
        app.state.stem_hydration_s3 = None
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_hydration_module_state():
    """The route module keeps process-wide in-flight/error registries; clear
    them around every test so tests cannot leak into each other."""
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()
    stem_index._last_refresh_attempt_mono.clear()
    stem_index._last_refresh_error.clear()
    stems_module.stem_hydration.OPEN_DECKS._open.clear()
    stems_module.stem_hydration.OPEN_DECKS._served_at.clear()
    yield
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()
    stem_index._last_refresh_attempt_mono.clear()
    stem_index._last_refresh_error.clear()
    stems_module.stem_hydration.OPEN_DECKS._open.clear()
    stems_module.stem_hydration.OPEN_DECKS._served_at.clear()


@pytest.mark.requirement("STEM-15")
def test_manifest_route_enqueues_and_returns_immediately(tmp_path: Path):
    """D2: the manifest GET must never block on the part fetch.

    [if] a bundle is remote-only [then] GET /stems enqueues hydration, returns at once, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
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
    """[if] a remote-only part is requested [then] the route waits then serves it, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
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
    exactly as it did before this feature.

    [if] no hydration deps are bound [then] routes fall back to old behavior, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    with _client(stems_dir, data_dir=data_dir) as client:
        manifest = client.get("/api/v1/tracks/absent/stems")
        part = client.get("/api/v1/tracks/absent/stems/vocals")
    assert manifest.status_code == 200
    assert manifest.json()["status"] == "unavailable"
    assert manifest.json()["hydrating"] is False
    assert part.status_code == 404


@pytest.mark.requirement("STEM-31")
def test_not_in_index_fails_loud_when_hydration_is_armed(tmp_path: Path):
    """When hydration is configured, a stable_id absent from the index must
    answer HTTP 502, never the ordinary unavailable envelope.

    [if] hydration is armed and unindexed [then] the manifest route answers 502, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {})  # empty index cached

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/truly-nowhere/stems")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_BUNDLE_NOT_INDEXED"


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

    [if] hydration fails for an indexed bundle [then] the next GET surfaces 502, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
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
    """[if] hydration fails for a part [then] the route answers 502, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "broken-track")
    entry["vocals.wav"] = "f" * 64  # never actually pushed
    save_cached_index(data_dir, {"broken-track": entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/broken-track/stems/vocals")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"


@pytest.mark.requirement("STEM-16")
def test_incomplete_index_entry_fails_loud_on_both_routes(tmp_path: Path):
    """An index entry with no manifest.json hash cannot be hydrated. Both
    routes must answer 502, never hydrating-forever or an ordinary 404.

    MUTATION TARGET: return "unavailable" for this case in hydrate_one and
    the manifest poll never leaves hydrating=True, the part route answers 404.

    [if] an index entry has no manifest hash [then] both routes answer 502, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "headless-track")
    del entry["manifest.json"]
    save_cached_index(data_dir, {"headless-track": entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        part = client.get("/api/v1/tracks/headless-track/stems/vocals")
        manifest = client.get("/api/v1/tracks/headless-track/stems")
    assert part.status_code == 502
    assert part.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"
    assert manifest.status_code == 502
    assert manifest.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"


@pytest.mark.requirement("STEM-17")
def test_openapi_part_route_declares_hydration_timeout_503() -> None:
    """[if] openapi.json is rebuilt for the part route [then] it declares 503/502, [else stop]."""
    openapi = json.loads(Path("apps/webui/openapi.json").read_text(encoding="utf-8"))
    part_route = openapi["paths"]["/api/v1/tracks/{stable_id}/stems/{part}"]["get"]
    assert "503" in part_route["responses"]
    assert "502" in part_route["responses"]


# --- deck-open/close parity endpoints ----------------------------------------


@pytest.mark.requirement("STEM-17")
def test_openapi_stem_unavailable_includes_hydrating_field() -> None:
    """[if] openapi.json is rebuilt for StemUnavailableOut [then] declares hydrating, [else stop]"""
    openapi = json.loads(Path("apps/webui/openapi.json").read_text(encoding="utf-8"))
    schema = openapi["components"]["schemas"]["StemUnavailableOut"]
    assert "hydrating" in schema["properties"]


def _assets_client(
    *,
    data_dir: Path,
    hydration_cfg: CloudConfig | None = None,
    hydration_s3: InMemoryAssetS3 | None = None,
) -> TestClient:
    app = FastAPI()
    app.state.stem_hydration_data_dir = data_dir
    if hydration_cfg is not None and hydration_s3 is not None:
        app.state.stem_hydration_source = DirectR2Source(
            cfg=hydration_cfg, s3=hydration_s3
        )
        app.state.stem_hydration_cfg = hydration_cfg
        app.state.stem_hydration_s3 = hydration_s3
    else:
        app.state.stem_hydration_source = None
        app.state.stem_hydration_cfg = None
        app.state.stem_hydration_s3 = None
    app.include_router(stems_assets_router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.requirement("STEM-20")
def test_bulk_hydrate_http_writes_under_request_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /stems/bulk-hydrate must honor the request body's data_dir stems path.

    [if] the request names a custom data_dir [then] bulk-hydrate writes there, [else stop].
    """
    data_dir = tmp_path / "custom-data"
    default_stems_dir = tmp_path / "must-stay-empty"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "http-track")
    save_cached_index(data_dir, {"http-track": entry})

    monkeypatch.setattr(
        "apps.stems.artifacts.DEFAULT_STEMS_DIR", default_stems_dir
    )
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    with _assets_client(data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.post(
            "/api/v1/stems/bulk-hydrate",
            json={
                "stable_ids": ["http-track"],
                "budget_bytes": 10**9,
                "data_dir": str(data_dir),
            },
        )
    assert resp.status_code == 200
    assert (data_dir / "state" / "stems" / "http-track" / "manifest.json").exists()
    assert not (default_stems_dir / "http-track" / "manifest.json").exists()


@pytest.mark.requirement("STEM-24")
def test_manifest_route_corrupt_index_cache_returns_502(tmp_path: Path):
    """[if] index cache is corrupt, no local bundle [then] manifest answers 502, [else stop]"""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    cache_path = local_index_cache_path(data_dir)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json at all", encoding="utf-8")

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/missing-track/stems")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_INDEX_CORRUPT"


@pytest.mark.requirement("STEM-24")
def test_part_route_corrupt_index_cache_returns_502(tmp_path: Path):
    """[if] index cache is corrupt, no local bundle [then] part route answers 502, [else stop]"""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    cache_path = local_index_cache_path(data_dir)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json at all", encoding="utf-8")

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/missing-track/stems/vocals")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_INDEX_CORRUPT"


@pytest.mark.requirement("STEM-24")
def test_bulk_hydrate_corrupt_index_cache_returns_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the local index cache is corrupt [then] bulk-hydrate answers 502, [else stop]"""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    cache_path = local_index_cache_path(data_dir)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json at all", encoding="utf-8")

    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    with _assets_client(data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.post(
            "/api/v1/stems/bulk-hydrate",
            json={
                "stable_ids": ["any-track"],
                "budget_bytes": 10**9,
                "data_dir": str(data_dir),
            },
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_INDEX_CORRUPT"


@pytest.mark.requirement("STEM-24")
def test_local_bundle_ignores_corrupt_index_cache(tmp_path: Path):
    """[if] a valid local bundle exists [then] a corrupt index cache is never read, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    stable_id = "local-track"
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes(stable_id))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())

    cache_path = local_index_cache_path(data_dir)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json at all", encoding="utf-8")

    with _client(stems_dir, data_dir=data_dir) as client:
        resp = client.get(f"/api/v1/tracks/{stable_id}/stems")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") != "unavailable"
    assert body["stable_id"] == stable_id


@pytest.mark.requirement("STEM-14")
def test_deck_open_close_endpoints_update_the_registry(tmp_path: Path):
    """[if] deck-open then deck-close are POSTed [then] OPEN_DECKS flips open/closed, [else stop]"""
    from apps.cloud.stem_hydration import OPEN_DECKS

    with _client(tmp_path / "stems", data_dir=tmp_path / "data") as client:
        client.post("/api/v1/tracks/deck-track/stems/deck-open")
        assert OPEN_DECKS.is_open("deck-track")
        client.post("/api/v1/tracks/deck-track/stems/deck-close")
        assert not OPEN_DECKS.is_open("deck-track")


# --- cold-cache index refresh (STEM-26) ------------------------------------


class _IndexGetFailingS3(InMemoryAssetS3):
    """Raises on get_object for the stem index key only."""

    def get_object(self, bucket: str, key: str):
        if key == INDEX_OBJECT_KEY:
            raise TimeoutError("simulated index fetch failure")
        return super().get_object(bucket, key)


class _TrackingGetS3(InMemoryAssetS3):
    """Records get_object calls for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls: list[tuple[str, str]] = []

    def get_object(self, bucket: str, key: str):
        self.get_calls.append((bucket, key))
        return super().get_object(bucket, key)


@pytest.mark.requirement("STEM-26")
def test_manifest_route_refreshes_cold_cache_and_enqueues(tmp_path: Path):
    """No local cache file: first manifest GET refreshes from R2 and enqueues.

    [if] no local index cache exists [then] the first GET refreshes from R2, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "cold-cache-track"
    entry = _seed_bundle(s3, cfg, stable_id)
    publish_index(cfg, s3, {stable_id: entry})
    assert not local_index_cache_path(data_dir).is_file()

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get(f"/api/v1/tracks/{stable_id}/stems")
    assert resp.status_code == 200
    assert resp.json()["hydrating"] is True
    assert local_index_cache_path(data_dir).is_file()


@pytest.mark.requirement("STEM-26")
def test_manifest_route_surfaces_refresh_failure(tmp_path: Path):
    """[if] the cold-cache refresh from R2 fails [then] manifest answers 502, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _IndexGetFailingS3()
    assert not local_index_cache_path(data_dir).is_file()

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/unrelated-track/stems")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "STEM_INDEX_REFRESH_FAILED"


@pytest.mark.requirement("STEM-26")
def test_manifest_route_warm_cache_does_not_refetch(tmp_path: Path):
    """A warm local cache (even empty of this stable_id) must not refetch R2.

    [if] a local index cache exists [then] repeated manifest GETs never refetch R2, [else stop].
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _TrackingGetS3()
    save_cached_index(data_dir, {})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        first = client.get("/api/v1/tracks/missing-track/stems")
        second = client.get("/api/v1/tracks/missing-track/stems")
    assert first.status_code == 502
    assert first.json()["detail"]["code"] == "STEM_BUNDLE_NOT_INDEXED"
    assert second.status_code == 502
    assert second.json()["detail"]["code"] == "STEM_BUNDLE_NOT_INDEXED"
    index_gets = [c for c in s3.get_calls if c[1] == INDEX_OBJECT_KEY]
    assert index_gets == []


# --- transport failure classification (STEM-27) ------------------------------


@pytest.mark.requirement("STEM-27")
def test_part_route_returns_502_on_transport_failure(tmp_path: Path):
    """[if] R2 transport fails fetching a part [then] the route answers 502 twice, [else stop]."""
    from tests.cloudsync.test_stem_hydration import _TransportFailingAssetS3

    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    stable_id = "transport-fail-track"
    base_s3 = InMemoryAssetS3()
    entry = _seed_bundle(base_s3, cfg, stable_id)
    fail_digest = entry["vocals.wav"]
    fail_key = (cfg.audio_bucket, asset_object_key(fail_digest))
    s3 = _TransportFailingAssetS3(fail_key=fail_key)
    for bucket, key in base_s3.store:
        s3.store[(bucket, key)] = base_s3.store[(bucket, key)]
    save_cached_index(data_dir, {stable_id: entry})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        first = client.get(f"/api/v1/tracks/{stable_id}/stems/vocals")
        second = client.get(f"/api/v1/tracks/{stable_id}/stems/vocals")
    assert first.status_code == 502
    assert first.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"
    assert second.status_code == 502
    assert second.json()["detail"]["code"] == "STEM_BUNDLE_HYDRATION_FAILED"


# --- serve-TTL eviction protection (STEM-30) --------------------------------


@pytest.mark.requirement("STEM-30")
def test_manifest_route_marks_bundle_recently_served(tmp_path: Path):
    """[if] manifest serves a local bundle [then] it marks it recently-served, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    stable_id = "served-track"
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes(stable_id))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())

    with _client(stems_dir, data_dir=data_dir) as client:
        resp = client.get(f"/api/v1/tracks/{stable_id}/stems")
    assert resp.status_code == 200
    assert stable_id in stems_module.stem_hydration.OPEN_DECKS.open_ids()
    assert not stems_module.stem_hydration.OPEN_DECKS.is_open(stable_id)
