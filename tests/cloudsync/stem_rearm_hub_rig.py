"""Shared rig for STEM-32 acceptance tests: real hub subprocess + hit counter.

``MDT_HUB_TEST_STEM_INDEX_HIT_FILE`` on the hub counts ``get_stem_index``
entries. ``MDT_HUB_TEST_STEM_INDEX_JSON`` serves a fixed index without R2.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from apps.cloud import policy
from apps.sync_hub import client, config as sync_config, hub_deploy
from apps.sync_hub.transport import HttpTransport
from tests.cloudsync.test_hub_deploy import (
    AMBIENT_ENV_TO_DROP,
    REPO_ROOT,
    _init_data_dir,
    _uv,
    _wait_for_hub,
)


@dataclass(frozen=True)
class BootedStemHub:
    url: str
    data_dir: Path
    proc: subprocess.Popen[bytes]
    log_path: Path
    hit_file: Path


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hub_env(
    *,
    allowed_hosts: str | None,
    hit_file: Path,
    index_json: Path | None,
    deny_index: bool = False,
) -> dict[str, str]:
    dropped = (*AMBIENT_ENV_TO_DROP,)
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env.update(hub_deploy.HUB_ENV)
    env["MDT_SYNC_CREDENTIAL_MODE"] = "observe"
    env["MDT_HUB_TEST_STEM_INDEX_HIT_FILE"] = str(hit_file)
    if deny_index:
        env["MDT_HUB_TEST_STEM_INDEX_DENY"] = "1"
    else:
        env.pop("MDT_HUB_TEST_STEM_INDEX_DENY", None)
    if index_json is not None:
        env["MDT_HUB_TEST_STEM_INDEX_JSON"] = str(index_json)
    else:
        env.pop("MDT_HUB_TEST_STEM_INDEX_JSON", None)
    if allowed_hosts is None:
        env.pop("MUSIC_DJ_ALLOWED_HOSTS", None)
    else:
        env["MUSIC_DJ_ALLOWED_HOSTS"] = allowed_hosts
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        env.pop(name, None)
    return env


def boot_stem_hub(
    root: Path,
    *,
    allowed_hosts: str | None = "127.0.0.1",
    index_json: Path | None = None,
    deny_index: bool = False,
    name: str = "hub",
) -> BootedStemHub:
    data_dir = root / name / hub_deploy.CFG.DATA_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    if not (data_dir / "state" / "state.db").is_file():
        _init_data_dir(data_dir)
    port = _free_loopback_port()
    hit_file = root / f"{name}-stem-index-hits"
    hit_file.unlink(missing_ok=True)
    argv = hub_deploy.hub_serve_argv(uv=_uv(), data_dir=data_dir, port=port)
    log_path = root / f"{name}.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=_hub_env(
                allowed_hosts=allowed_hosts,
                hit_file=hit_file,
                index_json=index_json,
                deny_index=deny_index,
            ),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    hub = BootedStemHub(
        f"http://127.0.0.1:{port}",
        data_dir,
        proc,
        log_path,
        hit_file,
    )
    _wait_for_hub(hub)
    return hub


def stop_stem_hub(hub: BootedStemHub) -> None:
    hub.proc.terminate()
    hub.proc.wait(timeout=30)


def read_hit_count(hub: BootedStemHub) -> int:
    if not hub.hit_file.is_file():
        return 0
    return int(hub.hit_file.read_text(encoding="utf-8").strip())


def write_index_json(path: Path, stable_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                stable_id: {
                    "manifest.json": "a" * 64,
                    "vocals.wav": "b" * 64,
                    "drums.wav": "c" * 64,
                    "bass.wav": "d" * 64,
                    "other.wav": "e" * 64,
                }
            }
        ),
        encoding="utf-8",
    )


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
