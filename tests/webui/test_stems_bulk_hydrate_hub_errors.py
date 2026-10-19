"""Bulk-hydrate hub transport failure contracts (issue #2978, STEM-35)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
    def get(
        self, path: str, params: Mapping[str, str | Sequence[str]]
    ) -> dict[str, Any]:
        raise SyncTransportError(f"GET {path} failed: Connection refused")

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raise SyncTransportError(f"POST {path} failed: Connection refused")


class _CountingPresignTransport:
    def __init__(
        self,
        *,
        presign_by_stable_id: dict[str, dict[str, Any]],
        fail_after: int,
    ) -> None:
        self._presign_by_stable_id = presign_by_stable_id
        self._fail_after = fail_after
        self._calls = 0

    def get(
        self, path: str, params: Mapping[str, str | Sequence[str]]
    ) -> dict[str, Any]:
        self._calls += 1
        if self._calls > self._fail_after:
            raise refused(f"GET {path}", 503, '{"detail":{"code":"HUB_DOWN"}}')
        stable_id = str(params["stable_id"])
        return self._presign_by_stable_id[stable_id]

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
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


class _MonthlyCapTransport:
    """A hub transport whose presign refuses with the monthly-cap code."""

    def __init__(self, status_code: int, code: str) -> None:
        self._status_code = status_code
        self._code = code

    def get(
        self, path: str, params: Mapping[str, str | Sequence[str]]
    ) -> dict[str, Any]:
        detail = json.dumps({"detail": {"code": self._code}})
        raise refused(f"GET {path}", self._status_code, detail)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raise SyncTransportError(f"unexpected POST {path}")


def _cap_bulk_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int, code: str
) -> tuple[int, dict[str, object]]:
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "capped-track")
    save_cached_index(data_dir, {"capped-track": entry})
    source = HubPresignedSource(
        data_dir=data_dir,
        hub_url="http://hub.invalid",
        machine_id="machine-1",
        bearer=None,
        transport=_MonthlyCapTransport(status_code, code),
    )
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: source,
    )
    with _hub_assets_client(data_dir=data_dir, source=source) as client:
        resp = client.post(
            "/api/v1/stems/bulk-hydrate",
            json={
                "stable_ids": ["capped-track"],
                "budget_bytes": 10**9,
                "data_dir": str(data_dir),
            },
        )
    payload = resp.json()
    detail = payload.get("detail")
    assert isinstance(detail, dict), payload
    return resp.status_code, detail


def test_bulk_hydrate_cap_refusal_answers_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the bulk route flattens a spent month to 502 [then] broken.

    Codex round 6: `_bulk_hydrate_stems_impl` catches StemSourceError and
    `_raise_stem_source_error` mapped every non-unreachable code to 502, so
    the agent-native bulk route retried a refusal that cannot change.
    """
    status, body = _cap_bulk_response(
        tmp_path, monkeypatch, 429, "CLOUDSTEMS_MONTHLY_CAP_REACHED"
    )
    assert status == 429, body
    assert body["code"] == "CLOUDSTEMS_MONTHLY_CAP_REACHED"


def test_bulk_hydrate_cap_unset_refusal_answers_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator-fixable refusal keeps its own code at the bulk route."""
    status, body = _cap_bulk_response(
        tmp_path, monkeypatch, 503, "CLOUDSTEMS_MONTHLY_CAP_UNSET"
    )
    assert status == 503, body
    assert body["code"] == "CLOUDSTEMS_MONTHLY_CAP_UNSET"


def test_bulk_hydrate_size_probe_failure_names_the_size_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a size-probe 503 names the presign endpoint [then] broken.

    Codex round 7: `bulk_hydrate` probes `bundle_remote_size()` through
    `/api/v1/sync/stems/bundle-size` before any presign, so a hub that is
    unreachable during the probe reported the PRESIGN endpoint in
    `detail.endpoint`. The endpoint now comes from the failing operation.
    """
    from apps.cloud.hub_stem_client import STEM_BUNDLE_SIZE_PATH

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
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["endpoint"] == STEM_BUNDLE_SIZE_PATH
