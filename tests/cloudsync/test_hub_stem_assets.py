"""Hub presigned stem asset endpoints and spoke hydration (STEM-31, #2630)."""
from __future__ import annotations

import hashlib
import json
import threading
import wave
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud import asset_store, stem_index
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_source import (
    STEM_BUNDLE_NOT_INDEXED,
    STEM_BUNDLE_PRESIGN_FAILED,
    STEM_HUB_UNREACHABLE,
    DirectR2Source,
    HubPresignedSource,
)
from apps.cloud.stem_hydration import hydrate_one
from apps.sync_hub import client
from apps.webui.server.routes.stems import router as stems_router
from apps.webui.server.stem_artifacts import load_stem_bundle
from tests.cloudsync.conftest import InMemoryAssetS3, _enroll_hub_app
from tests.cloudsync.enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("STEM-31")


@pytest.fixture(autouse=True)
def _hub_observe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")


def _wav_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(b"\x00\x00" * 16)
    return buf.getvalue()


def _manifest_bytes(stable_id: str) -> bytes:
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {
            "vocals": "vocals.wav",
            "drums": "drums.wav",
            "bass": "bass.wav",
            "other": "other.wav",
        },
    }
    return (json.dumps(manifest) + "\n").encode("utf-8")


def _cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct",
        r2_access_key_id="id",
        r2_secret_access_key="secret",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="host",
        bind_host="127.0.0.1",
    )


def _seed_bundle(s3: InMemoryAssetS3, cfg: CloudConfig, stable_id: str) -> dict[str, str]:
    files = {
        "manifest.json": _manifest_bytes(stable_id),
        "vocals.wav": _wav_bytes(),
        "drums.wav": _wav_bytes(),
        "bass.wav": _wav_bytes(),
        "other.wav": _wav_bytes(),
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[filename] = digest
    return entry


def test_hub_presign_response_has_no_r2_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "cred-check"
    entry = _seed_bundle(s3, cfg, stable_id)
    stem_index.publish_index(cfg, s3, {stable_id: entry})
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )

    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        response = hub_http.get(
            "/api/v1/sync/stems/bundle-presign",
            params={"machine_id": machine_id, "stable_id": stable_id},
        )
    assert response.status_code == 200
    body = response.text
    assert "R2_ACCESS_KEY_ID" not in body
    assert "R2_SECRET_ACCESS_KEY" not in body
    assert cfg.r2_secret_access_key not in body


class _AssetHttpHandler(BaseHTTPRequestHandler):
    bodies: dict[str, bytes] = {}

    def do_GET(self) -> None:
        digest = self.path.rsplit("/", 1)[-1]
        body = self.bodies.get(digest)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def asset_http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AssetHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_installed_style_hydration_via_hub_presign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_http_server: str,
) -> None:
    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    stems_dir = spoke_dir / "state" / "stems"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "installed-track"
    entry = _seed_bundle(s3, cfg, stable_id)
    stem_index.publish_index(cfg, s3, {stable_id: entry})
    _AssetHttpHandler.bodies = {
        digest: s3.store[(cfg.audio_bucket, asset_object_key(digest))][0]
        for digest in entry.values()
    }
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets.asset_store.presign_url",
        lambda _cfg, digest, _expiry: (
            f"{asset_http_server}/assets/{digest[:2]}/{digest}"
        ),
    )
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)

    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        from apps.sync_hub import config as sync_config

        sync_config.write_config(
            spoke_dir,
            sync_config.CloudSyncConfig(
                enabled=True,
                hub_url="http://hub.invalid",
                machine_name="spoke",
            ),
        )
        source = HubPresignedSource(
            data_dir=spoke_dir,
            hub_url="http://hub.invalid",
            machine_id=machine_id,
            bearer=None,
            transport=transport,
        )
        source.refresh_index(spoke_dir, force=True)
        index = stem_index.load_cached_index(spoke_dir)
        outcome = hydrate_one(
            stable_id,
            data_dir=spoke_dir,
            source=source,
            index=index,
            stems_dir=stems_dir,
        )
    assert outcome.status == "hydrated"
    load_stem_bundle(stable_id, stems_dir=stems_dir)


def test_manifest_route_502_when_hub_unreachable(tmp_path: Path) -> None:
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    from apps.sync_hub import config as sync_config

    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(
            enabled=True,
            hub_url="http://127.0.0.1:9",
            machine_name="spoke",
        ),
    )
    assert not stem_index.local_index_cache_path(data_dir).is_file()

    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    from apps.shared.state.machine_identity import get_or_create_machine_id

    machine_id = get_or_create_machine_id(data_dir)
    app.state.stem_hydration_source = HubPresignedSource(
        data_dir=data_dir,
        hub_url="http://127.0.0.1:9",
        machine_id=machine_id,
        bearer=None,
    )
    app.include_router(stems_router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/tracks/any-track/stems")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == STEM_HUB_UNREACHABLE


def test_build_argv_uses_r2_first_worker_when_hydration_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.stems.job import R2_FIRST_WORKER_SCRIPT, build_argv

    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "indexed-only"
    entry = _seed_bundle(s3, cfg, stable_id)
    stem_index.save_cached_index(data_dir, {stable_id: entry})
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "modal",
    )
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )
    argv = build_argv({"stable_ids": [stable_id], "data_dir": str(data_dir), "tier": "M"})
    assert R2_FIRST_WORKER_SCRIPT in argv[1]
    assert "--with" not in argv


def test_hub_rejects_invalid_stable_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )
    with TestClient(_enroll_hub_app(tmp_path / "hub")) as hub_http:
        transport = TestClientTransport(hub_http)
        client.run_sync(tmp_path / "spoke", "http://hub.invalid", transport=transport)
        machine_id = (tmp_path / "spoke" / "machine-id").read_text(encoding="utf-8").strip()
        response = hub_http.get(
            "/api/v1/sync/stems/bundle-presign",
            params={"machine_id": machine_id, "stable_id": "../escape"},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == STEM_BUNDLE_PRESIGN_FAILED


def test_r2_first_worker_subprocess_hydrates_indexed_track(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_http_server: str,
) -> None:
    """The real worker subprocess must hydrate an indexed track without uv."""
    import os
    import subprocess
    import sys

    import uvicorn

    from apps.stems.job import PROGRESS_TRACK_KEY, R2_FIRST_WORKER_SCRIPT
    from apps.sync_hub.transport import HttpTransport
    from tests.cloudsync.conftest import free_port
    from tests.waits import start_uvicorn_in_thread

    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "subprocess-track"
    entry = _seed_bundle(s3, cfg, stable_id)
    stem_index.publish_index(cfg, s3, {stable_id: entry})
    _AssetHttpHandler.bodies = {
        digest: s3.store[(cfg.audio_bucket, asset_object_key(digest))][0]
        for digest in entry.values()
    }
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets.asset_store.presign_url",
        lambda _cfg, digest, _expiry: (
            f"{asset_http_server}/assets/{digest[:2]}/{digest}"
        ),
    )
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)

    port = free_port()
    hub_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        _enroll_hub_app(hub_dir),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server, thread = start_uvicorn_in_thread(config, what="the live stem hub")
    try:
        client.run_sync(spoke_dir, hub_url, transport=HttpTransport(hub_url), name="spoke")
        from apps.sync_hub import config as sync_config

        sync_config.write_config(
            spoke_dir,
            sync_config.CloudSyncConfig(
                enabled=True,
                hub_url=hub_url,
                machine_name="spoke",
            ),
        )
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[2] / R2_FIRST_WORKER_SCRIPT),
                "--data-dir",
                str(spoke_dir),
                "--stable-id",
                stable_id,
                "--tier",
                "M",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[2]),
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "MDT_SYNC_CREDENTIAL_MODE": "observe",
            },
            check=False,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)

    assert result.returncode == 0, result.stderr
    assert PROGRESS_TRACK_KEY in result.stdout
    assert (spoke_dir / "state" / "stems" / stable_id / "manifest.json").is_file()


def test_manifest_route_502_when_indexed_bundle_missing_from_hub_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    indexed_id = "indexed-track"
    entry = _seed_bundle(s3, cfg, indexed_id)
    stem_index.save_cached_index(data_dir, {indexed_id: entry})
    from apps.sync_hub import config as sync_config

    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(
            enabled=True,
            hub_url="http://hub.invalid",
            machine_name="spoke",
        ),
    )
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    from apps.shared.state.machine_identity import get_or_create_machine_id

    machine_id = get_or_create_machine_id(data_dir)
    app.state.stem_hydration_source = HubPresignedSource(
        data_dir=data_dir,
        hub_url="http://hub.invalid",
        machine_id=machine_id,
        bearer=None,
    )
    app.include_router(stems_router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/tracks/not-in-index/stems")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == STEM_BUNDLE_NOT_INDEXED
