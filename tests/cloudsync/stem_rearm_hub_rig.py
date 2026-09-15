"""Shared rig for STEM-32 acceptance tests: in-process hub + index fetcher injection."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from apps.cloud import policy, stem_index
from apps.sync_hub import client, hub_deploy
from apps.sync_hub import config as sync_config
from apps.sync_hub import service as sync_service
from apps.sync_hub.transport import HttpTransport
from tests.cloudsync.conftest import free_port
from tests.cloudsync.test_hub_deploy import _init_data_dir
from tests.waits import start_uvicorn_in_thread


class _CountingStemIndexFetcher:
    """Injectable stem index source that counts every fetch attempt."""

    def __init__(self, index: dict[str, dict[str, str]] | None = None) -> None:
        self._index = index
        self.hit_count = 0

    def __call__(self) -> stem_index.StemAssetIndex:
        self.hit_count += 1
        if self._index is None:
            raise stem_index.StemIndexError("test hub has no stem index configured")
        return self._index


@dataclass
class BootedStemHub:
    url: str
    data_dir: Path
    server: uvicorn.Server
    thread: threading.Thread
    fetcher: _CountingStemIndexFetcher


def stem_index_payload(stable_id: str) -> dict[str, dict[str, str]]:
    return {
        stable_id: {
            "manifest.json": "a" * 64,
            "vocals.wav": "b" * 64,
            "drums.wav": "c" * 64,
            "bass.wav": "d" * 64,
            "other.wav": "e" * 64,
        }
    }


def _stem_hub_app(
    data_dir: Path,
    *,
    name: str,
    fetcher: _CountingStemIndexFetcher,
) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(data_dir))
    app.state.sync_hub_data_dir = str(data_dir)
    app.state.sync_hub_machine_name = name
    app.state.stem_index_fetcher = fetcher
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


def boot_stem_hub(
    root: Path,
    *,
    port: int | None = None,
    index: dict[str, dict[str, str]] | None = None,
    name: str = "hub",
) -> BootedStemHub:
    data_dir = root / name / hub_deploy.CFG.DATA_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    if not (data_dir / "state" / "state.db").is_file():
        _init_data_dir(data_dir)
    bind_port = port if port is not None else free_port()
    fetcher = _CountingStemIndexFetcher(index)
    config = uvicorn.Config(
        _stem_hub_app(data_dir, name=name, fetcher=fetcher),
        host="127.0.0.1",
        port=bind_port,
        log_level="warning",
    )
    server, thread = start_uvicorn_in_thread(config, what=f"stem rearm hub {name}")
    return BootedStemHub(
        f"http://127.0.0.1:{bind_port}",
        data_dir,
        server,
        thread,
        fetcher,
    )


def stop_stem_hub(hub: BootedStemHub) -> None:
    hub.server.should_exit = True
    hub.thread.join(timeout=10.0)


def read_hit_count(hub: BootedStemHub) -> int:
    return hub.fetcher.hit_count


def enroll_spoke(spoke_dir: Path, hub_url: str) -> None:
    spoke_dir.mkdir(parents=True, exist_ok=True)
    client.run_sync(spoke_dir, hub_url, transport=HttpTransport(hub_url), name="spoke")
    sync_config.write_config(
        spoke_dir,
        sync_config.CloudSyncConfig(
            enabled=True,
            hub_url=hub_url,
            machine_name="spoke",
        ),
    )


def use_cloud_mode(monkeypatch) -> None:
    monkeypatch.setenv(policy.MODE_ENV, "cloud")
    monkeypatch.setattr(policy, "CFG", policy.load_policy())


def build_spoke_app(spoke_dir: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    state_dir = spoke_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_db_path = state_dir / "state.db"
    if not state_db_path.is_file():
        from apps.shared.state import db as state_db

        state_db.open_rw(state_db_path).close()
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    use_cloud_mode(monkeypatch)
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
        stem_hydration=True,
    )
    app.state.stems_dir = state_dir / "stems"
    client = TestClient(app, base_url="http://127.0.0.1")
    return app, client
