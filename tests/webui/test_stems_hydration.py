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
import threading
import time
import wave
from concurrent.futures import wait as futures_wait
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
def _reset_hydration_module_state(tmp_path: Path):
    """The route module keeps process-wide in-flight/error registries; clear
    them around every test so tests cannot leak into each other.

    Clearing alone is not enough: a test that only GETs the manifest leaves its
    hydration running on the module executor. Pytest deletes that test's
    tmp_path at teardown (``tmp_path_retention_policy = "failed"``), the
    worker's verify step then fails, and its late ``_LAST_HYDRATE_ERROR`` write
    lands in the NEXT test, which answers 502 for a bundle it just seeded (B20,
    issue #6044). So teardown first waits for every hydration the test started.
    Requesting ``tmp_path`` orders this teardown before tmp_path's removal.
    """
    del tmp_path
    _clear_hydration_state()
    yield
    _drain_hydrations()
    _clear_hydration_state()


def _clear_hydration_state() -> None:
    with stems_module._INFLIGHT_LOCK:
        stems_module._INFLIGHT.clear()
        stems_module._INFLIGHT_FILE_TOTALS.clear()
        stems_module._LAST_HYDRATE_ERROR.clear()
    stem_index._last_refresh_attempt_mono.clear()
    stem_index._last_refresh_error.clear()
    stems_module.stem_hydration.OPEN_DECKS._open.clear()
    stems_module.stem_hydration.OPEN_DECKS._served_at.clear()
    stems_module.stem_hydration.OPEN_DECKS._read_holds.clear()


def _drain_hydrations(timeout_s: float = 30.0) -> None:
    """Wait for every hydration still in flight; fail loud if one hangs."""
    with stems_module._INFLIGHT_LOCK:
        pending = list(stems_module._INFLIGHT.values())
    _done, not_done = futures_wait(pending, timeout=timeout_s)
    assert not not_done, f"{len(not_done)} stem hydration(s) outlived the test by {timeout_s}s"


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


@pytest.mark.requirement("STEM-42")
def test_manifest_route_marks_then_waits_out_an_eviction_claim(tmp_path: Path):
    """[if] an eviction claim holds the bundle lock when a deck asks for the manifest [then] it is marked at once and read after, [else stop].

    Codex P1 on #6820.

    MUTATION TARGET: mark without ``_hold_for_deck``'s wait and the route
    answers while the claim still holds the lock.
    """
    import threading

    from apps.cloud import stem_cache_budget
    from apps.cloud.stem_upload_queue import exclusive_file_lock

    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    stable_id = "claimed-track"
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes(stable_id))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())
    registry = stems_module.stem_hydration.OPEN_DECKS
    statuses: list[int] = []

    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        with exclusive_file_lock(stem_cache_budget.eviction_claim_lock_path(data_dir, stable_id)):
            request = threading.Thread(
                target=lambda: statuses.append(client.get(f"/api/v1/tracks/{stable_id}/stems").status_code)
            )
            request.start()
            deadline = time.monotonic() + 30
            while stable_id not in registry.open_ids():
                assert time.monotonic() < deadline, "the route never marked the bundle"
                time.sleep(0.01)
            time.sleep(1.0)
            assert statuses == [], "the route read the bundle while the claim held the lock"
        request.join(timeout=60)

    assert statuses == [200]


@pytest.mark.requirement("STEM-42")
def test_manifest_poll_answers_while_a_hydration_holds_the_bundle_lock(tmp_path: Path):
    """[if] a hydration holds the bundle lock for its R2 fetch [then] the manifest route still answers at once, [else stop].

    Codex P1 on #6821: the documented immediate hydrating answer must not
    wait on the network.

    MUTATION TARGET: make ``hold_against_eviction`` wait on the bundle lock
    and this request hangs until the lock is released.
    """
    import threading

    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import bundle_lock_path

    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    data_dir = tmp_path / "data"
    stable_id = "being-hydrated"
    statuses: list[int] = []

    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        with exclusive_file_lock(bundle_lock_path(data_dir, stable_id)):
            request = threading.Thread(
                target=lambda: statuses.append(client.get(f"/api/v1/tracks/{stable_id}/stems").status_code)
            )
            request.start()
            request.join(timeout=10)
            assert statuses == [200], "the manifest route waited on the hydration's bundle lock"


def _served_bundle(stems_dir: Path, stable_id: str) -> Path:
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(_manifest_bytes(stable_id))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())
    return bundle_dir


@pytest.mark.requirement("STEM-42")
@pytest.mark.parametrize(
    "path",
    ["/api/v1/tracks/{sid}/stems", "/api/v1/tracks/{sid}/stems/vocals", "/api/v1/tracks/{sid}/stems/vocals/waveform"],
)
def test_a_read_of_a_corrupt_bundle_leaves_no_eviction_lease(tmp_path: Path, path: str):
    """[if] a stems read fails on a corrupt bundle [then] the bundle is not left marked served, [else stop].

    A request that served no bytes must not keep a broken bundle from
    eviction for the six-hour served window (Codex on #6820).

    MUTATION TARGET: drop the ``withdraw()`` on the failure path and the
    corrupt bundle stays in ``open_ids()``.
    """
    stems_dir = tmp_path / "stems"
    stable_id = "corrupt-track"
    _served_bundle(stems_dir, stable_id)
    (stems_dir / stable_id / "vocals.wav").write_bytes(b"not a wav")

    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        resp = client.get(path.format(sid=stable_id))

    assert resp.status_code in (404, 422), resp.text
    assert stable_id not in stems_module.stem_hydration.OPEN_DECKS.open_ids()


@pytest.mark.requirement("STEM-42")
def test_an_unavailable_manifest_answer_leaves_no_eviction_lease(tmp_path: Path):
    """[if] the manifest route answers unavailable because no bundle is on disk [then] the track is not left marked served, [else stop].

    A request that served no bundle must not shield whatever a hydration
    later writes for the six-hour served window (Codex on #6820).

    MUTATION TARGET: drop the ``withdraw()`` on the unavailable answer and
    the track stays in ``open_ids()``.
    """
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    stable_id = "no-bundle-yet"

    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        resp = client.get(f"/api/v1/tracks/{stable_id}/stems")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "unavailable", resp.json()
    assert stable_id not in stems_module.stem_hydration.OPEN_DECKS.open_ids()


@pytest.mark.requirement("STEM-42")
def test_a_failed_read_keeps_the_lease_an_earlier_good_read_took(tmp_path: Path):
    """[if] a good read marked the bundle and a later read of an unknown part fails [then] the bundle stays marked, [else stop].

    Overshoot control: undoing a failed read must restore, not erase.
    """
    stems_dir = tmp_path / "stems"
    stable_id = "good-then-bad"
    _served_bundle(stems_dir, stable_id)

    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        assert client.get(f"/api/v1/tracks/{stable_id}/stems").status_code == 200
        assert client.get(f"/api/v1/tracks/{stable_id}/stems/not-a-part").status_code == 404

    assert stable_id in stems_module.stem_hydration.OPEN_DECKS.open_ids()


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


@pytest.mark.requirement("STEM-58")
@pytest.mark.parametrize("route", ["manifest", "part", "waveform"])
def test_reads_during_back_to_back_swaps_never_find_the_bundle_gone(tmp_path: Path, route: str):
    """[if] bundles are swapped in over and over while a deck reads [then] every read finds a bundle, [else stop].

    ``_hold_for_deck`` only waits out a claim already under way; a swap
    that starts after it would empty the bundle path under the read, which
    the deck sees as no stems, a 404 part or a 404 waveform (Codex on
    #6820). Real swaps through ``replace_bundle_dir``, real lock files and
    a real app, no stand-ins.

    MUTATION TARGET: make ``stem_cache_budget.read_barrier`` hold nothing.
    """
    import threading

    from apps.stems.bundle_lock import bundle_lock, replace_bundle_dir

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    stable_id = "swapped-track"
    # A waveform decodes its part outside the barrier, so give it audio long
    # enough for a decode to overlap a swap (the case its one retry is for).
    part_bytes = _wav_bytes(frames=44_100 * 2) if route == "waveform" else _wav_bytes()

    def write_bundle(directory: Path) -> None:
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_bytes(_manifest_bytes(stable_id))
        for part in ("vocals", "drums", "bass", "other"):
            (directory / f"{part}.wav").write_bytes(part_bytes)

    bundle = stems_dir / stable_id
    write_bundle(bundle)
    staging_root = data_dir / "state" / ".stem-publish"
    staging_root.mkdir(parents=True)
    path = {
        "manifest": f"/api/v1/tracks/{stable_id}/stems",
        "part": f"/api/v1/tracks/{stable_id}/stems/vocals",
        "waveform": f"/api/v1/tracks/{stable_id}/stems/vocals/waveform",
    }[route]
    done = threading.Event()
    swaps: list[int] = []

    def keep_swapping() -> None:
        try:
            for i in range(4000):
                if done.is_set():
                    return
                staging = staging_root / f"{stable_id}-{i}"
                write_bundle(staging)
                with bundle_lock(bundle):
                    replace_bundle_dir(staging, bundle)
                swaps.append(i)
        finally:
            done.set()

    misses: list[str] = []
    reads = 0
    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        writer = threading.Thread(target=keep_swapping)
        writer.start()
        deadline = time.monotonic() + 120
        try:
            while not done.is_set() and (reads < 1500 or len(swaps) <= 50) and time.monotonic() < deadline:
                response = client.get(path)
                reads += 1
                if response.status_code != 200:
                    misses.append(f"{response.status_code} {response.json()}")
                elif route == "manifest" and response.json().get("status") == "unavailable":
                    misses.append(f"200 {response.json()}")
        finally:
            done.set()
            writer.join(timeout=120)

    assert len(swaps) > 50 and reads > 50, f"control: the swaps and reads overlapped ({len(swaps)} swaps, {reads} reads)"
    assert not misses, f"{len(misses)} of {reads} reads found no bundle: {misses[:3]}"


def _half_tone_wav(*, frames: int, loud_half: int) -> bytes:
    """A stereo WAV that is silent except in half ``loud_half`` (0 or 1), so envelopes tell versions apart."""
    import io
    import struct

    half = frames // 2
    samples = bytearray()
    for i in range(frames):
        value = 12_000 if (i >= half) == bool(loud_half) else 0
        samples += struct.pack("<hh", value, value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(bytes(samples))
    return buf.getvalue()


def test_an_instrumental_envelope_never_mixes_two_bundle_versions(tmp_path: Path):
    """[if] bundles alternate between two versions under instrumental reads [then] every envelope served or cached is one version's, [else stop].

    The instrumental envelope decodes bass, then other, outside the read
    barrier. A swap between the two decodes used to return, and cache, the
    peak of one version's bass with the next version's other (Codex on
    #6820). Version A sounds only in its first half and B only in its
    second, so a mix of the two is loud throughout and matches neither.

    MUTATION TARGET: make ``stem_waveform.pin_waveform_inputs`` link nothing
    AND ``_require_unchanged`` check nothing (either one alone holds this).
    """
    import threading

    from apps.stems.artifacts import load_stem_bundle
    from apps.stems.bundle_lock import bundle_lock, replace_bundle_dir
    from apps.stems.stem_waveform import build_stem_waveform_payload

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    stable_id = "alternating-track"
    versions = [_half_tone_wav(frames=44_100 // 2, loud_half=half) for half in (0, 1)]

    def write_bundle(directory: Path, version: int) -> None:
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_bytes(_manifest_bytes(stable_id))
        for part in ("vocals", "drums", "bass", "other"):
            (directory / f"{part}.wav").write_bytes(versions[version])

    expected: list[list[float]] = []
    for version in (0, 1):
        reference = tmp_path / f"reference-{version}" / stable_id
        write_bundle(reference, version)
        bundle = load_stem_bundle(stable_id, stems_dir=reference.parent)
        expected.append(build_stem_waveform_payload(bundle, "instrumental")["envelope"])
    assert expected[0] != expected[1], "control: the two versions have different envelopes"
    mixed = [max(a, b) for a, b in zip(expected[0], expected[1], strict=True)]
    assert mixed not in expected, "control: a mix of the versions matches neither"

    bundle_path = stems_dir / stable_id
    write_bundle(bundle_path, 0)
    staging_root = data_dir / "state" / ".stem-publish"
    staging_root.mkdir(parents=True)
    done = threading.Event()
    swaps: list[int] = []

    def keep_swapping() -> None:
        try:
            for i in range(200_000):
                if done.is_set():
                    return
                staging = staging_root / f"{stable_id}-{i}"
                write_bundle(staging, (i + 1) % 2)
                with bundle_lock(bundle_path):
                    replace_bundle_dir(staging, bundle_path)
                swaps.append(i)
        finally:
            done.set()

    wrong: list[str] = []
    served = 0
    path = f"/api/v1/tracks/{stable_id}/stems/instrumental/waveform"
    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        client.app.state.data_dir = data_dir
        writer = threading.Thread(target=keep_swapping)
        writer.start()
        # Read until both sides have done enough to overlap, not until the
        # reads alone run out: on a slow disk 60 reads can finish inside 26 swaps.
        deadline = time.monotonic() + 120
        try:
            while not done.is_set() and (served < 60 or len(swaps) <= 50) and time.monotonic() < deadline:
                response = client.get(path)
                if response.status_code != 200:
                    wrong.append(f"{response.status_code} {response.json()}")
                    continue
                served += 1
                if response.json()["envelope"] not in expected:
                    wrong.append("served an envelope matching neither version")
        finally:
            done.set()
            writer.join(timeout=120)

    cached = sorted((data_dir / "state" / "stem-waveform-cache").rglob("*.json"))
    wrong.extend(
        f"cached an envelope matching neither version: {entry.name}"
        for entry in cached
        if json.loads(entry.read_text(encoding="utf-8"))["envelope"] not in expected
    )
    assert len(swaps) > 50 and served >= 60, f"control: the swaps and reads overlapped ({len(swaps)} swaps, {served} reads)"
    assert cached, "control: the reads populated the envelope cache"
    assert not wrong, f"{len(wrong)} wrong answers of {served} served: {wrong[:3]}"


def test_a_part_replaced_under_its_decode_is_refused_and_an_untouched_one_is_not(tmp_path: Path):
    """[if] a part file is replaced after its stat [then] the guard raises; [else] an untouched one passes, [else stop].

    MUTATION TARGET: compare only size and mtime, or always raise.
    """
    import os

    from apps.stems.stem_waveform import StemPartChangedError, _require_unchanged

    part = tmp_path / "bass.wav"
    part.write_bytes(_wav_bytes())
    before = part.stat()
    _require_unchanged(part, before)  # control: untouched passes

    replacement = tmp_path / "bass.next.wav"
    replacement.write_bytes(_wav_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, part)
    with pytest.raises(StemPartChangedError):
        _require_unchanged(part, before)

    part.unlink()
    with pytest.raises(StemPartChangedError):
        _require_unchanged(part, before)


_LEAK_SID = "slow-leak-track"


class _GatedFailingS3(InMemoryAssetS3):
    """A bucket whose part fetch blocks until the test releases it, then fails.

    The gate replaces a fixed sleep, so the test controls exactly when the
    hydration it started finishes instead of racing a timer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        self.entered.set()
        if not self.release.wait(timeout=30.0):
            raise TimeoutError("the leak test never released its gated fetch")
        raise RuntimeError("gated fetch failed after the request that started it")


@pytest.mark.requirement("STEM-15")
def test_teardown_drain_waits_for_a_hydration_that_outlives_its_request(tmp_path: Path):
    """B20 (#6044): a hydration started by one test must finish inside that test.

    The manifest GET returns while the part fetch is still blocked, so the
    hydration is registered and unfinished when the request ends. Without the
    drain its late ``_LAST_HYDRATE_ERROR`` write landed in the NEXT test, which
    is how a cold run's part route answered 502. One self-contained test, so a
    shard split or a ``-k`` selection cannot leave half of it unchecked.

    [if] the reset fixture's drain runs [then] the hydration is done and its error recorded before the next clear, [else stop].

    MUTATION TARGET: make ``_drain_hydrations`` return without waiting.
    """
    cfg = _cfg()
    s3 = _GatedFailingS3()
    data_dir = tmp_path / "data"
    save_cached_index(data_dir, {_LEAK_SID: _seed_bundle(s3, cfg, _LEAK_SID)})
    with _client(
        tmp_path / "stems", data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3
    ) as client:
        resp = client.get(f"/api/v1/tracks/{_LEAK_SID}/stems")
    assert resp.status_code == 200 and resp.json().get("hydrating") is True
    assert s3.entered.wait(timeout=10.0), "the hydration never reached the part fetch"
    with stems_module._INFLIGHT_LOCK:
        future = stems_module._INFLIGHT.get(_LEAK_SID)
    assert future is not None, "the GET registered no hydration"
    assert not future.done(), "the hydration finished before the request returned"

    # Release from another thread only after the drain has started waiting.
    threading.Timer(0.2, s3.release.set).start()
    _drain_hydrations()
    assert future.done(), "the drain returned while the hydration was still running"
    with stems_module._INFLIGHT_LOCK:
        recorded = dict(stems_module._LAST_HYDRATE_ERROR)
    assert _LEAK_SID in recorded, f"the drained hydration recorded no error: {recorded}"

    # What the next test sees: a clear after the drain stays clear.
    _clear_hydration_state()
    with stems_module._INFLIGHT_LOCK:
        assert stems_module._LAST_HYDRATE_ERROR == {}
