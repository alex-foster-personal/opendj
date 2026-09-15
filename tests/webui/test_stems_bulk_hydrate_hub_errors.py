"""Bulk-hydrate hub transport failure contracts (issue #2978, STEM-35)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_index import save_cached_index
from apps.cloud.stem_source import HubPresignedSource
from apps.sync_hub.transport import SyncTransportError, refused
from apps.webui.server.routes.stems import router as stems_router
from apps.webui.server.routes.stems_assets import router as stems_assets_router
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.webui.test_stems_hydration import _assets_client, _cfg, _seed_bundle


class _UnreachableHubTransport:
    def get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        raise SyncTransportError(f"GET {path} failed: Connection refused")

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        raise SyncTransportError(f"POST {path} failed: Connection refused")


class _CountingPresignTransport:
    def __init__(
        self,
        *,
        presign_by_stable_id: dict[str, dict[str, object]],
        fail_after: int,
    ) -> None:
        self._presign_by_stable_id = presign_by_stable_id
        self._fail_after = fail_after
        self._calls = 0

    def get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        self._calls += 1
        if self._calls > self._fail_after:
            raise refused(f"GET {path}", 503, '{"detail":{"code":"HUB_DOWN"}}')
        stable_id = params["stable_id"]
        return self._presign_by_stable_id[stable_id]

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        raise SyncTransportError(f"unexpected POST {path}")


def _hub_stems_client(
    *,
    data_dir: Path,
    source: HubPresignedSource,
) -> TestClient:
    app = FastAPI()
    stems_dir = data_dir / "state" / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_source = source
    app.include_router(stems_router, prefix="/api/v1")
    return TestClient(app)


def _hub_assets_client(
    *,
    data_dir: Path,
    source: HubPresignedSource,
) -> TestClient:
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_source = source
    app.include_router(stems_assets_router, prefix="/api/v1")
    return TestClient(app)


def _presign_payload(
    cfg: CloudConfig, entry: dict[str, str], bodies: dict[str, bytes]
) -> dict[str, object]:
    from apps.cloud.asset_store import presign_url

    return {
        "files": [
            {
                "filename": filename,
                "content_hash": digest,
                "size_bytes": len(bodies[digest]),
                "url": presign_url(cfg, digest),
            }
            for filename, digest in entry.items()
        ]
    }


@pytest.mark.requirement("STEM-35")
def test_bulk_hydrate_hub_unreachable_before_work_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] hub unreachable before bulk work [then] 503 SYNC_HUB_UNREACHABLE, [else stop]."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "hub-track")
    save_cached_index(data_dir, {"hub-track": entry})
    source = HubPresignedSource(
        data_dir=data_dir,
        hub_url="http://127.0.0.1:9",
        machine_id="machine-1",
        bearer=None,
        transport=_UnreachableHubTransport(),
    )
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: source,
    )

    with _hub_assets_client(data_dir=data_dir, source=source) as client:
        resp = client.post(
            "/api/v1/stems/bulk-hydrate",
            json={
                "stable_ids": ["hub-track"],
                "budget_bytes": 10**9,
                "data_dir": str(data_dir),
            },
        )
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "SYNC_HUB_UNREACHABLE"
        assert detail["endpoint"]
        assert "Connection refused" in detail["message"]
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.requirement("STEM-35")
def test_bulk_hydrate_hub_5xx_mid_pass_returns_200_partial_hub_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] hub 5xx after bytes fetched [then] 200 partial hub_error rows, [else stop]."""
    from tests.cloudsync.presign_asset_rig import serve_presigned_assets

    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_ids = ["track-a", "track-b", "track-c"]
    index: dict[str, dict[str, str]] = {}
    bodies: dict[str, bytes] = {}
    for stable_id in stable_ids:
        entry = _seed_bundle(s3, cfg, stable_id)
        index[stable_id] = entry
        for digest in entry.values():
            bodies[digest] = s3.store[(cfg.audio_bucket, asset_object_key(digest))][0]
    save_cached_index(data_dir, index)
    monkeypatch.setattr(
        "apps.stems.cli.stems_dir",
        lambda _data_dir: data_dir / "state" / "stems",
    )

    with serve_presigned_assets(cfg, bodies):
        presign_by_id = {
            stable_id: _presign_payload(cfg, entry, bodies)
            for stable_id, entry in index.items()
        }
        transport = _CountingPresignTransport(
            presign_by_stable_id=presign_by_id,
            fail_after=2,
        )
        source = HubPresignedSource(
            data_dir=data_dir,
            hub_url="http://hub.invalid",
            machine_id="machine-1",
            bearer=None,
            transport=transport,
        )
        monkeypatch.setattr(
            "apps.cloud.stem_source.resolve_stem_hydration_source",
            lambda _data_dir: source,
        )
        with _hub_assets_client(data_dir=data_dir, source=source) as client:
            resp = client.post(
                "/api/v1/stems/bulk-hydrate",
                json={
                    "stable_ids": stable_ids,
                    "budget_bytes": 10**9,
                    "data_dir": str(data_dir),
                },
            )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["bytes_fetched"] > 0
    assert payload["fetched"][0]["stable_id"] == "track-a"
    assert {row["stable_id"] for row in payload["skipped"]} == {"track-b", "track-c"}
    assert all(row["status"] == "hub_error" for row in payload["skipped"])


@pytest.mark.requirement("STEM-35")
def test_part_route_hub_unreachable_returns_503_sync_hub_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] hub unreachable during async part hydration [then] 503 SYNC_HUB_UNREACHABLE, [else stop]."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "hub-part-track")
    save_cached_index(data_dir, {"hub-part-track": entry})
    source = HubPresignedSource(
        data_dir=data_dir,
        hub_url="http://127.0.0.1:9",
        machine_id="machine-1",
        bearer=None,
        transport=_UnreachableHubTransport(),
    )
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: source,
    )

    with _hub_stems_client(data_dir=data_dir, source=source) as client:
        resp = client.get("/api/v1/tracks/hub-part-track/stems/vocals")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == "SYNC_HUB_UNREACHABLE"
    assert detail["endpoint"]
    assert "Connection refused" in detail["message"]


@pytest.mark.requirement("STEM-35")
def test_bulk_hydrate_healthy_hub_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] hub path is not used and source is healthy [then] bulk-hydrate unchanged, [else stop]."""
    data_dir = tmp_path / "custom-data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    from apps.cloud.stem_source import DirectR2Source

    entry = _seed_bundle(s3, cfg, "http-track")
    save_cached_index(data_dir, {"http-track": entry})
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
