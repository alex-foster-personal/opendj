"""Hub presigned stem asset endpoints and spoke hydration (STEM-31, #2630).

[if] a presigned download fails its sha256 check [then] no partial bundle is written, [else stop].
"""
from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud import stem_index
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_hydration import hydrate_one
from apps.cloud.stem_source import (
    STEM_BUNDLE_NOT_INDEXED,
    STEM_BUNDLE_PRESIGN_FAILED,
    STEM_HYDRATION_NOT_ARMED,
    STEM_MONTHLY_CAP_REACHED,
    STEM_MONTHLY_CAP_UNSET,
    DirectR2Source,
    HubPresignedSource,
    SourceMode,
    StemSourceError,
    hub_transport_failure_kind,
)
from apps.shared.state import db as state_db
from apps.sync_hub import client
from apps.webui.server.app import create_app
from apps.webui.server.routes.stems import router as stems_router
from apps.webui.server.sqlite_backend import SqliteBackend
from apps.webui.server.stem_artifacts import load_stem_bundle
from tests.cloudsync.conftest import InMemoryAssetS3, _enroll_hub_app
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.presign_asset_rig import serve_presigned_assets

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


def _presign_bodies(
    s3: InMemoryAssetS3, cfg: CloudConfig, entry: dict[str, str]
) -> dict[str, bytes]:
    return {
        digest: s3.store[(cfg.audio_bucket, asset_object_key(digest))][0]
        for digest in entry.values()
    }


def _insert_track(state_db_path: Path, stable_id: str) -> None:
    conn = state_db.open_rw(state_db_path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (stable_id, "inferred", "Test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()


def _hub_spoke_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stable_id: str,
) -> tuple[Path, Path, CloudConfig, InMemoryAssetS3, dict[str, str], str]:
    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, stable_id)
    stem_index.publish_index(cfg, s3, {stable_id: entry})
    monkeypatch.setattr(
        "apps.sync_hub.service_stem_assets._hub_r2_clients",
        lambda: (cfg, s3),
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
    return spoke_dir, hub_dir, cfg, s3, entry, machine_id


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
    presign = response.json()
    for file_entry in presign["files"]:
        assert file_entry["url"].startswith("https://")
        assert "X-Amz-Signature=" in file_entry["url"]


def test_installed_style_hydration_via_hub_presign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stable_id = "installed-track"
    spoke_dir, _hub_dir, cfg, s3, entry, machine_id = _hub_spoke_setup(
        tmp_path, monkeypatch, stable_id=stable_id
    )
    stems_dir = spoke_dir / "state" / "stems"
    bodies = _presign_bodies(s3, cfg, entry)
    with TestClient(_enroll_hub_app(tmp_path / "hub")) as hub_http:
        transport = TestClientTransport(hub_http)
        with serve_presigned_assets(cfg, bodies):
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


def test_presigned_sha256_mismatch_writes_no_partial_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stable_id = "hash-mismatch"
    spoke_dir, _hub_dir, cfg, s3, entry, machine_id = _hub_spoke_setup(
        tmp_path, monkeypatch, stable_id=stable_id
    )
    stems_dir = spoke_dir / "state" / "stems"
    bodies = _presign_bodies(s3, cfg, entry)
    bad_digest = entry["vocals.wav"]
    bodies = dict(bodies)
    bodies[bad_digest] = b"wrong-bytes-not-a-wav"
    with TestClient(_enroll_hub_app(tmp_path / "hub")) as hub_http:
        transport = TestClientTransport(hub_http)
        with serve_presigned_assets(cfg, bodies):
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
    assert outcome.status == "error"
    assert outcome.reason is not None
    assert "presigned body for" in outcome.reason
    assert "not the requested digest" in outcome.reason
    assert not (stems_dir / stable_id).exists()


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
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SYNC_HUB_UNREACHABLE"


def test_manifest_route_502_stem_hydration_not_armed_when_configured_unarmed(
    tmp_path: Path,
) -> None:
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
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_unarmed_reason = "hub unreachable at boot-arm"
    app.include_router(stems_router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/tracks/any-track/stems")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == STEM_HYDRATION_NOT_ARMED


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


def test_build_argv_never_falls_through_to_modal_when_hub_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: if resolve returns a hub source, build_argv must stay on
    the R2-first worker even when the indexed fetch would later fail."""
    from apps.stems.job import (
        LOCAL_WORKER_SCRIPT,
        R2_FIRST_WORKER_SCRIPT,
        WORKER_SCRIPT,
        build_argv,
    )
    from apps.sync_hub.machine_credentials import CREDENTIAL_PREFIX

    data_dir = tmp_path / "data"
    stable_id = "hub-indexed"
    stem_index.save_cached_index(
        data_dir, {stable_id: {"manifest.json": "a" * 64, "vocals.wav": "b" * 64}}
    )
    hub_source = HubPresignedSource(
        data_dir=data_dir,
        hub_url="http://127.0.0.1:9",
        machine_id="machine-1",
        bearer=f"{CREDENTIAL_PREFIX}test-token",
    )
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "modal",
    )
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: hub_source,
    )
    argv = build_argv({"stable_ids": [stable_id], "data_dir": str(data_dir), "tier": "M"})
    assert R2_FIRST_WORKER_SCRIPT in argv[1]
    assert WORKER_SCRIPT not in argv
    assert LOCAL_WORKER_SCRIPT not in argv


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
) -> None:
    """The real worker subprocess must hydrate an indexed track without uv."""
    import os
    import subprocess
    import sys

    import uvicorn

    from apps.stems.job import PROGRESS_TRACK_KEY, R2_FIRST_WORKER_SCRIPT
    from apps.sync_hub.transport import HttpTransport
    from tests.cloudsync.conftest import free_port
    from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

    stable_id = "subprocess-track"
    spoke_dir, hub_dir, cfg, s3, entry, _machine_id = _hub_spoke_setup(
        tmp_path, monkeypatch, stable_id=stable_id
    )
    bodies = _presign_bodies(s3, cfg, entry)
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
        from apps.sync_hub import config as sync_config

        sync_config.write_config(
            spoke_dir,
            sync_config.CloudSyncConfig(
                enabled=True,
                hub_url=hub_url,
                machine_name="spoke",
            ),
        )
        client.run_sync(spoke_dir, hub_url, transport=HttpTransport(hub_url), name="spoke")
        with serve_presigned_assets(cfg, bodies):
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
        stop_uvicorn_in_thread(server, thread, what="the live stem hub")

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


def test_route_level_hub_hydration_manifest_part_and_track_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed-style route stack: manifest enqueues, part serves bytes, list
    reports stems_available from the cached hub index."""
    from apps.adapters.rekordbox import config as rb_config

    stable_id = "route-level-track"
    spoke_dir, hub_dir, cfg, s3, entry, machine_id = _hub_spoke_setup(
        tmp_path, monkeypatch, stable_id=stable_id
    )
    state_dir = spoke_dir / "state"
    state_db_path = state_dir / "state.db"
    conn = state_db.open_rw(state_db_path)
    conn.close()
    _insert_track(state_db_path, stable_id)
    stems_dir = state_dir / "stems"
    bodies = _presign_bodies(s3, cfg, entry)
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        with serve_presigned_assets(cfg, bodies):
            app = create_app(
                backend=SqliteBackend(state_db_path),
                bind_host="127.0.0.1",
                hostname="test-host",
                state_db_path=str(state_db_path),
                mount_frontend=False,
                stem_hydration=False,
            )
            app.state.stems_dir = stems_dir
            app.state.stem_hydration_data_dir = spoke_dir
            app.state.stem_hydration_source = HubPresignedSource(
                data_dir=spoke_dir,
                hub_url="http://hub.invalid",
                machine_id=machine_id,
                bearer=None,
                transport=transport,
            )
            stem_index.save_cached_index(spoke_dir, {stable_id: entry})
            with TestClient(app) as client:
                list_resp = client.get("/api/v1/tracks")
                assert list_resp.status_code == 200
                rows = list_resp.json()["items"]
                row = next(item for item in rows if item["stable_id"] == stable_id)
                assert row["stems_available"] is True

                manifest_resp = client.get(f"/api/v1/tracks/{stable_id}/stems")
                assert manifest_resp.status_code == 200
                assert manifest_resp.json().get("hydrating") is True

                part_resp = client.get(f"/api/v1/tracks/{stable_id}/stems/vocals")
                assert part_resp.status_code == 200
                assert part_resp.headers["content-type"].startswith("audio/")


def test_r2_first_worker_indexed_fetch_failure_never_falls_through_to_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: an indexed track whose presigned fetch fails must not
    spawn the Modal/local compute worker for that id."""
    from scripts import stems_r2_first_worker as worker

    stable_id = "indexed-fetch-fail"
    spoke_dir, hub_dir, _cfg, _s3, entry, machine_id = _hub_spoke_setup(
        tmp_path, monkeypatch, stable_id=stable_id
    )
    stem_index.save_cached_index(spoke_dir, {stable_id: entry})
    compute_calls: list[list[str]] = []

    def _spy_compute(stable_ids: list[str], **kwargs: object) -> int:
        compute_calls.append(list(stable_ids))
        return 0

    monkeypatch.setattr(worker, "_run_compute", _spy_compute)
    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        hub_source = HubPresignedSource(
            data_dir=spoke_dir,
            hub_url="http://hub.invalid",
            machine_id=machine_id,
            bearer=None,
            transport=transport,
        )
        monkeypatch.setattr(
            worker,
            "resolve_stem_hydration_source",
            lambda _data_dir: hub_source,
        )
        monkeypatch.setattr(
            "apps.stems.hydrate_runner.resolve_stem_hydration_source",
            lambda _data_dir: hub_source,
        )
        # Real presign from hub, but presigned GET targets R2 with no loopback server.
        with pytest.raises(SystemExit) as exc_info:
            worker.main(
                [
                    "--data-dir",
                    str(spoke_dir),
                    "--stable-id",
                    stable_id,
                    "--tier",
                    "M",
                ]
            )
    assert exc_info.value.code != 0
    assert compute_calls == []
    assert not (spoke_dir / "state" / "stems" / stable_id).exists()


class _MonthlyCapSource:
    """A hub source whose hub refused with 429 CLOUDSTEMS_MONTHLY_CAP_REACHED."""

    mode = "hub_presigned"

    def __init__(self, code: str = STEM_MONTHLY_CAP_REACHED, message: str | None = None):
        self._code = code
        self._message = message or (
            "bundle presign: hub answered HTTP 429 (CLOUDSTEMS_MONTHLY_CAP_REACHED)"
        )

    def _refuse(self) -> NoReturn:
        raise StemSourceError(self._code, self._message)

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool:
        self._refuse()

    def refresh_error(self, data_dir: Path) -> str | None:
        return None

    def bundle_remote_size(
        self, file_hashes: dict[str, str], *, stable_id: str
    ) -> int | None:
        self._refuse()

    def fetch_bundle_files(
        self, *, stable_id: str, file_hashes: dict[str, str], tmp_dir: Path
    ) -> int:
        self._refuse()


def test_a_monthly_cap_refusal_is_not_a_transport_failure() -> None:
    """Codex round 3, P2: the cap code must not be read as "hub is down".

    ``hub_transport_failure_kind`` drives bulk hydration's degrade-and-continue
    and the route's 502/503 mapping. A spent month is neither: retrying cannot
    help before the month rolls over, so classification must be None.
    """
    assert (
        hub_transport_failure_kind(
            StemSourceError(STEM_MONTHLY_CAP_REACHED, "hub answered HTTP 429")
        )
        is None
    )
    assert (
        hub_transport_failure_kind(
            StemSourceError(STEM_MONTHLY_CAP_UNSET, "hub answered HTTP 503")
        )
        is None
    )


def test_spoke_route_answers_429_with_the_cap_code_preserved(
    tmp_path: Path,
) -> None:
    """[if] the spoke answers 502 for a spent month [then] broken.

    The hub's 429 used to be flattened to STEM_HUB_INDEX_FAILED (502) at the
    stems route, which a client reads as a broken hub and retries. It must
    answer 429 and keep CLOUDSTEMS_MONTHLY_CAP_REACHED as the code.
    """
    app = FastAPI()
    app.state.stems_dir = tmp_path / "stems"
    app.state.stem_hydration_data_dir = tmp_path
    app.state.stem_hydration_source = _MonthlyCapSource()
    app.include_router(stems_router, prefix="/api/v1")
    with TestClient(app) as client:
        manifest = client.get("/api/v1/tracks/capped-track/stems")
        manifest_data = client.get("/api/v1/tracks/capped-track/stems/state")
    assert manifest.status_code == 429, manifest.text
    assert manifest.json()["detail"]["code"] == STEM_MONTHLY_CAP_REACHED
    # A stateless read still names the state rather than raising.
    assert manifest_data.status_code == 200


def test_spoke_route_keeps_the_unset_cap_refusal_distinct(tmp_path: Path) -> None:
    """CLOUDSTEMS_MONTHLY_CAP_UNSET is an operator fix, not a 502 either."""
    app = FastAPI()
    app.state.stems_dir = tmp_path / "stems"
    app.state.stem_hydration_data_dir = tmp_path
    app.state.stem_hydration_source = _MonthlyCapSource(
        STEM_MONTHLY_CAP_UNSET,
        "bundle presign: hub answered HTTP 503 (CLOUDSTEMS_MONTHLY_CAP_UNSET)",
    )
    app.include_router(stems_router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/tracks/capped-track/stems")
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == STEM_MONTHLY_CAP_UNSET


def test_an_expired_cap_refusal_is_forgotten_at_the_utc_reset() -> None:
    """[if] a cached 429 outlives the month it names [then] broken.

    Codex round 4 on #7322: a background hydration records
    CLOUDSTEMS_MONTHLY_CAP_REACHED in _LAST_HYDRATE_ERROR, and the manifest
    and part routes raise a recorded error before re-enqueueing. With no
    expiry an app kept open across 00:00 UTC on the 1st kept answering the
    previous month's 429 for that track until an explicit hydrate POST or a
    restart. The record now carries an expiry and a stale one is dropped.
    """
    from datetime import UTC, datetime

    from apps.webui.server.routes import stems as stems_module

    stable = "reset-track"
    stems_module._LAST_HYDRATE_ERROR.pop(stable, None)
    stems_module._remember_hydrate_error(stable, STEM_MONTHLY_CAP_REACHED, "spent")
    recorded = stems_module._recorded_hydration_error(stable)
    assert recorded is not None
    assert recorded.code == STEM_MONTHLY_CAP_REACHED
    # Text naming no month is not guessed into one: it is rechecked soon.
    assert recorded.expires_at is not None
    assert recorded.expires_at <= datetime.now(UTC).timestamp() + (
        stems_module.CAP_UNNAMED_RECHECK_S
    )
    december = datetime(2026, 12, 15, 12, 0, tzinfo=UTC)
    december_text = "CloudStems monthly cap reached for 2026-12: 200 of 200 tracks"
    assert stems_module._cap_refusal_expiry(
        STEM_MONTHLY_CAP_REACHED, december, december_text
    ) == datetime(2027, 1, 1, tzinfo=UTC).timestamp()

    # The month the HUB refused decides the expiry, not when the spoke read
    # it: a refusal for December read on 1 January is already expired, while
    # the receipt-time fallback alone would cache it until 1 February (Sol
    # review of 61dd5ee997, P1).
    new_year = datetime(2027, 1, 1, 0, 0, 5, tzinfo=UTC)
    hub_text = "CloudStems monthly cap reached for 2026-12: 200 of 200 tracks"
    assert stems_module._cap_refusal_expiry(
        STEM_MONTHLY_CAP_REACHED, new_year, hub_text
    ) == datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    # A refusal whose text names no month (or an impossible one) is NOT cached
    # to the end of the receipt month: just after midnight that would replay a
    # previous month's refusal for the whole new month (Sol review of
    # 27f9acf396, P1). It is asked again after a short recheck instead.
    for unnamed in ("spent", "CloudStems monthly cap reached for 2026-13: x"):
        assert stems_module._cap_refusal_expiry(
            STEM_MONTHLY_CAP_REACHED, new_year, unnamed
        ) == new_year.timestamp() + stems_module.CAP_UNNAMED_RECHECK_S

    # An expired record is not returned and is dropped, so the next request
    # asks the hub again instead of replaying last month's refusal.
    stems_module._LAST_HYDRATE_ERROR[stable] = stems_module._HydrateError(
        STEM_MONTHLY_CAP_REACHED, "spent last month", expires_at=0.0
    )
    assert stems_module._recorded_hydration_error(stable) is None
    assert stable not in stems_module._LAST_HYDRATE_ERROR

    # An UNSET-allowance refusal is rechecked sooner: an operator can fix it.
    stems_module._LAST_HYDRATE_ERROR[stable] = stems_module._HydrateError(
        STEM_MONTHLY_CAP_UNSET, "no cap", expires_at=0.0
    )
    assert stems_module._recorded_hydration_error(stable) is None

    # A non-time-bound failure keeps the old behaviour: no expiry, still loud.
    stems_module._remember_hydrate_error(stable, STEM_BUNDLE_PRESIGN_FAILED, "bad")
    still = stems_module._recorded_hydration_error(stable)
    assert still is not None and still.expires_at is None
    stems_module._LAST_HYDRATE_ERROR.pop(stable, None)


class _CachedIndexCapSource:
    """A warm-cache hub source: index already present, the presign refuses."""

    mode: SourceMode = "hub_presigned"

    def __init__(self, code: str) -> None:
        self._code = code

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool:
        return True

    def refresh_error(self, data_dir: Path) -> str | None:
        return None

    def bundle_remote_size(
        self, file_hashes: dict[str, str], *, stable_id: str
    ) -> int | None:
        return None

    def fetch_bundle_files(
        self, *, stable_id: str, file_hashes: dict[str, str], tmp_dir: Path
    ) -> int:
        raise StemSourceError(
            self._code, f"bundle presign: hub answered 429 ({self._code})"
        )


def test_cached_index_presign_refusal_still_answers_429(tmp_path: Path) -> None:
    """[if] a warm cache turns a spent month into a 502 [then] broken.

    Codex round 5: with the index already cached, the refusal happens inside
    ``fetch_bundle_files``; ``hydrate_one`` used to return a CODE-LESS
    ``HydrationOutcome(status="error")``, so ``_run_hydration`` recorded
    STEM_BUNDLE_HYDRATION_FAILED and the part route answered 502. The code now
    survives the outcome.
    """
    entry = {"manifest.json": "a" * 64}
    app = FastAPI()
    data_dir = tmp_path / "spoke"
    data_dir.mkdir()
    stem_index.save_cached_index(data_dir, {"capped-track": entry})
    app.state.stems_dir = tmp_path / "stems"
    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_source = _CachedIndexCapSource(STEM_MONTHLY_CAP_REACHED)
    app.include_router(stems_router, prefix="/api/v1")
    with TestClient(app) as client:
        manifest = client.get("/api/v1/tracks/capped-track/stems")
        assert manifest.status_code == 200, manifest.text
        assert manifest.json().get("hydrating") is True
        part = client.get("/api/v1/tracks/capped-track/stems/vocals")
    assert part.status_code == 429, part.text
    assert part.json()["detail"]["code"] == STEM_MONTHLY_CAP_REACHED


def test_cached_index_cap_refusal_stops_a_bulk_run(tmp_path: Path) -> None:
    """A spent month must abort bulk hydration, not skip track by track."""
    from apps.cloud.stem_hydration import bulk_hydrate

    entry = {"manifest.json": "a" * 64}
    data_dir = tmp_path / "spoke"
    data_dir.mkdir()
    source = _CachedIndexCapSource(STEM_MONTHLY_CAP_REACHED)
    with pytest.raises(StemSourceError) as raised:
        bulk_hydrate(
            ["track-a", "track-b"],
            data_dir=data_dir,
            source=source,
            index={"track-a": entry, "track-b": entry},
            byte_budget=10_000,
            stems_dir=tmp_path / "stems",
        )
    assert raised.value.code == STEM_MONTHLY_CAP_REACHED


def test_part_route_contract_keeps_the_cap_unset_refusal() -> None:
    """[if] the part route's 503 forgets CLOUDSTEMS_MONTHLY_CAP_UNSET [then] broken.

    Codex round 5: ``STEM_PART_RESPONSES`` spread the hydration responses and
    then REPLACED the 503 entry, so the operator-fixable cap refusal vanished
    from openapi.json and api-types.ts. The entry is now merged.
    """
    from apps.webui.server.routes.stems import STEM_PART_RESPONSES

    description = str(STEM_PART_RESPONSES[503]["description"])
    assert STEM_MONTHLY_CAP_UNSET in description
    assert "SYNC_HUB_UNREACHABLE" in description
    assert "STEM_BUNDLE_HYDRATING" in description


def test_a_monthly_cap_refusal_is_logged_below_error(caplog: pytest.LogCaptureFixture) -> None:
    """[if] a spent allowance is logged as an ERROR (Sentry noise) [then] broken.

    Sol on f9e7f65676 (P2): a capped track was logged under the generic
    STEM_BUNDLE_HYDRATION_FAILED code at ERROR. A cap refusal is an answer
    from the hub; a real fetch failure must still be an ERROR (the control).
    """
    import logging

    from apps.webui.server.routes import stems as stems_module

    with caplog.at_level(logging.WARNING, logger=stems_module.log.name):
        stems_module._log_hydration_failure("t", STEM_MONTHLY_CAP_REACHED, "spent")
        stems_module._log_hydration_failure("t", STEM_MONTHLY_CAP_UNSET, "no cap")
        stems_module._log_hydration_failure("t", "STEM_BUNDLE_HYDRATION_FAILED", "boom")
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.WARNING, logging.WARNING, logging.ERROR], levels
