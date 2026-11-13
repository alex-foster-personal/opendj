"""The hub status verdict carries R2 serving capability (CLOUDSYNC-4686b).

Incident Thu 1 Oct 2026: a hub re-rendered without its Doppler wrap came up
with health 200 and verdict OK while every stem presign answered 503 for
hours. These tests boot the real sync router behind uvicorn on loopback and
read it with the real ``hub_deploy.probe_hub``; R2 is a loopback object
server reached through real presigned URLs (``MDT_ASSET_PRESIGN_HOST``), so
the capability is asserted by a presign that actually fetched something.

[if] assets are indexed and the hub cannot presign [then] the verdict is DEGRADED, [else stop].
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud import stem_index
from apps.cloud.asset_store import PRESIGN_HOST_ENV, PRESIGN_SCHEME_ENV
from apps.cloud.config import REQUIRED_VARS, CloudConfig
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import client as sync_client
from apps.sync_hub import hub_deploy, r2_capability
from apps.sync_hub import service as sync_service
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.cloudsync.presign_asset_rig import serve_presigned_assets_by_key
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CLOUDSYNC-4686b")

STABLE_ID: str = "track-with-assets"
WORDS_HASH: str = "c" * 64
R2_ENV: dict[str, str] = {
    "R2_ACCOUNT_ID": "acct",
    "R2_ACCESS_KEY_ID": "id",
    "R2_SECRET_ACCESS_KEY": "secret",
    "MUSIC_DJ_AUDIO_BUCKET": "test-audio",
}


@pytest.fixture(autouse=True)
def _hub_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub process with NO R2 variables: each test opts in to what it needs."""
    monkeypatch.setenv(machine_identity.IS_HUB_ENV, "1")
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")
    for name in (*REQUIRED_VARS, "MUSIC_DJ_AUDIO_BUCKET", PRESIGN_HOST_ENV, PRESIGN_SCHEME_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    path = tmp_path / "hub" / hub_deploy.CFG.DATA_DIR_NAME
    hub_deploy.init_hub_data_dir(path, env={machine_identity.IS_HUB_ENV: "1"}, name="r2-cap-hub")
    return path


def _hub_app(hub_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "r2-cap-hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


@contextmanager
def _live_hub(hub_dir: Path) -> Iterator[str]:
    """A FRESH app per block: a restarted hub process, with an empty probe cache."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    config = uvicorn.Config(_hub_app(hub_dir), host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the R2 capability hub")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def _index_lyric_asset(hub_dir: Path) -> None:
    conn = state_db.open_rw(sync_client.state_db_path(hub_dir))
    stamp = "2026-10-01T00:00:00Z"
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, 'inferred', 'Test', ?, ?)",
        (STABLE_ID, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO lyric_verdict (stable_id, verdict, pipeline_version, "
        "words_content_hash, computed_at, updated_at) VALUES (?, 'vocal', 'v1', ?, ?, ?)",
        (STABLE_ID, WORDS_HASH, stamp, stamp),
    )
    conn.commit()
    conn.close()


def _index_body(bundles: int) -> bytes:
    index = {f"track-{n}": {stem_index.MANIFEST_FILENAME: f"{n:064x}"} for n in range(1, bundles + 1)}
    return json.dumps({"schema_version": stem_index.INDEX_SCHEMA_VERSION, "stable_ids": index}).encode("utf-8")


@contextmanager
def _r2_serving(monkeypatch: pytest.MonkeyPatch, objects: dict[str, bytes]) -> Iterator[None]:
    """R2 variables present and a loopback bucket answering presigned GETs."""
    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    with serve_presigned_assets_by_key(CloudConfig.from_env(), objects):
        yield
    for name in R2_ENV:
        monkeypatch.delenv(name, raising=False)


def test_control_hub_without_assets_and_without_r2_stays_ok(hub_dir: Path) -> None:
    """if a hub that indexes nothing and never had R2 is not OK then broken"""
    with _live_hub(hub_dir) as url:
        probe = hub_deploy.probe_hub(url, hub_dir)
    assert probe.verdict == "OK", probe.reason
    assert probe.r2_state == "ABSENT"
    assert probe.indexed_lyric_assets == 0
    assert probe.indexed_stem_bundles is None  # never measured, and it says so


def test_degraded_when_r2_env_absent_but_lyric_assets_indexed(hub_dir: Path) -> None:
    """if a hub indexing lyric assets with no R2 variables reads OK then broken"""
    _index_lyric_asset(hub_dir)
    with _live_hub(hub_dir) as url:
        probe = hub_deploy.probe_hub(url, hub_dir)
    assert probe.verdict == "DEGRADED", probe.reason
    assert probe.r2_state == "ABSENT"
    assert probe.indexed_lyric_assets == 1
    assert "R2_ACCOUNT_ID" in probe.reason
    assert "cannot presign" in probe.reason
    assert probe.is_hub is True  # still the right hub: degraded, not critical


def test_ok_when_presign_fetches_then_degraded_once_r2_is_dropped(
    hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if dropping R2 from a hub that served 3 indexed bundles still reads OK then broken"""
    with _r2_serving(monkeypatch, {stem_index.INDEX_OBJECT_KEY: _index_body(3)}), _live_hub(hub_dir) as url:
        serving = hub_deploy.probe_hub(url, hub_dir)
    assert serving.verdict == "OK", serving.reason
    assert serving.r2_state == "SERVING"
    assert serving.indexed_stem_bundles == 3

    # The incident: the same data dir, restarted without the R2 variables.
    with _live_hub(hub_dir) as url:
        dropped = hub_deploy.probe_hub(url, hub_dir)
    assert dropped.verdict == "DEGRADED", dropped.reason
    assert dropped.r2_state == "ABSENT"
    assert dropped.indexed_stem_bundles == 3  # the last measured index outlives the process
    assert "3 stem bundle" in dropped.reason


def test_ok_with_r2_present_and_lyric_assets_indexed(hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """if a hub that CAN presign reads DEGRADED because assets are indexed then broken"""
    _index_lyric_asset(hub_dir)
    with _r2_serving(monkeypatch, {stem_index.INDEX_OBJECT_KEY: _index_body(1)}), _live_hub(hub_dir) as url:
        probe = hub_deploy.probe_hub(url, hub_dir)
    assert probe.verdict == "OK", probe.reason
    assert probe.r2_state == "SERVING"
    assert probe.indexed_lyric_assets == 1


def test_degraded_when_r2_refuses_the_presigned_fetch(hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """if variables that mint a URL the bucket refuses read OK then broken"""
    # An empty bucket server answers 404 with no NoSuchKey body: not an R2
    # "the index does not exist yet", so the mint proved nothing.
    with _r2_serving(monkeypatch, {}), _live_hub(hub_dir) as url:
        probe = hub_deploy.probe_hub(url, hub_dir)
    assert probe.verdict == "DEGRADED", probe.reason
    assert probe.r2_state == "FAILED"
    assert "HTTP 404" in probe.reason


def test_unknown_when_r2_cannot_be_reached(hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """if an unreachable R2 renders as a verdict instead of UNKNOWN then broken"""
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        dead_port = int(closed.getsockname()[1])
    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(PRESIGN_HOST_ENV, f"127.0.0.1:{dead_port}")
    monkeypatch.setenv(PRESIGN_SCHEME_ENV, "http")
    with _live_hub(hub_dir) as url:
        probe = hub_deploy.probe_hub(url, hub_dir)
    assert probe.verdict == "UNKNOWN", probe.reason
    assert probe.r2_state == "UNKNOWN"


def test_presigned_url_never_reaches_the_status_payload(hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """if the status body carries a signature or a credential then broken"""
    with _r2_serving(monkeypatch, {}), _live_hub(hub_dir) as url:
        own_id = machine_identity.machine_id_path(hub_dir).read_text().strip()
        body = json.dumps(hub_deploy._fetch_status(url, own_id))
    assert '"r2"' in body  # the field under test is present, so the absences below mean something
    for forbidden in ("X-Amz-Signature", "X-Amz-Credential", R2_ENV["R2_SECRET_ACCESS_KEY"]):
        assert forbidden not in body


def test_status_cli_exits_1_for_degraded_and_0_for_identity_only(
    hub_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if the status CLI exits 0 for a degraded hub, or --identity-only does not, then broken"""
    _index_lyric_asset(hub_dir)
    with _live_hub(hub_dir) as url:
        full = hub_deploy.main(["status", "--data-dir", str(hub_dir), "--url", url])
        full_out = capsys.readouterr().out
        identity = hub_deploy.main(["status", "--data-dir", str(hub_dir), "--url", url, "--identity-only"])
        identity_out = capsys.readouterr().out
    assert full == 1
    assert "[DEGRADED]" in full_out
    assert identity == 0
    assert "[OK] is_hub true" in identity_out


def test_a_status_body_without_r2_is_unknown_never_ok() -> None:
    """if a hub build that reports no R2 capability is read as OK then broken"""
    verdict, reason = r2_capability.verdict_from_status_body({"hub_machine_id": "x"})
    assert verdict == "UNKNOWN"
    assert "no r2 capability" in reason


def test_startup_logs_the_capability_it_measured(hub_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """if a hub that boots unable to presign indexed assets logs nothing then broken"""
    _index_lyric_asset(hub_dir)
    app = _hub_app(hub_dir)
    with caplog.at_level(logging.INFO, logger=r2_capability.__name__):
        capability = r2_capability.log_startup_capability(app.state)
    assert capability is not None
    assert capability.verdict == "DEGRADED"
    (record,) = [r for r in caplog.records if r.name == r2_capability.__name__]
    assert record.levelno == logging.ERROR
    assert "DEGRADED" in record.getMessage()


def test_startup_measures_nothing_and_says_nothing_on_a_spoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """if a spoke engine measures or logs hub R2 capability at boot then broken"""
    monkeypatch.delenv(machine_identity.IS_HUB_ENV)
    app = FastAPI()
    untouched = tmp_path / "spoke" / "state" / "state.db"
    app.state.state_db_path = str(untouched)
    with caplog.at_level(logging.DEBUG, logger=r2_capability.__name__):
        assert r2_capability.log_startup_capability(app.state) is None
    assert not untouched.parent.exists()
    assert [r for r in caplog.records if r.name == r2_capability.__name__] == []


def test_startup_with_an_unreadable_hub_flag_logs_and_does_not_block_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """if MDT_IS_HUB=yes makes the boot hook raise, or pass in silence, then broken"""
    monkeypatch.setenv(machine_identity.IS_HUB_ENV, "yes")
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    with caplog.at_level(logging.INFO, logger=r2_capability.__name__):
        assert r2_capability.log_startup_capability(app.state) is None
    (record,) = [r for r in caplog.records if r.name == r2_capability.__name__]
    assert record.levelno == logging.ERROR
    assert "MDT_IS_HUB='yes'" in record.getMessage()


def test_startup_on_a_hub_with_no_database_creates_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """if the boot hook mints a state DB that was not there then broken"""
    app = FastAPI()
    missing = tmp_path / "state" / "state.db"
    app.state.state_db_path = str(missing)
    with caplog.at_level(logging.INFO, logger=r2_capability.__name__):
        assert r2_capability.log_startup_capability(app.state) is None
    assert not missing.parent.exists()
    (record,) = [r for r in caplog.records if r.name == r2_capability.__name__]
    assert "not measured" in record.getMessage()


def test_status_from_a_non_hub_process_reports_no_r2_capability(hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """if a process without MDT_IS_HUB=1 reports an R2 capability verdict then broken"""
    _index_lyric_asset(hub_dir)
    monkeypatch.delenv(machine_identity.IS_HUB_ENV)
    with _live_hub(hub_dir) as url:
        own_id = machine_identity.machine_id_path(hub_dir).read_text().strip()
        body = hub_deploy._fetch_status(url, own_id)
    assert body["hub_machine_id"] == own_id  # the status call itself worked
    assert body["r2"] is None


def test_the_engine_lifespan_runs_the_startup_measurement(hub_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """if a hub engine boots without measuring and logging its R2 capability then broken"""
    _index_lyric_asset(hub_dir)
    db_path = sync_client.state_db_path(hub_dir)
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="r2-cap-hub",
        state_db_path=str(db_path),
        mount_frontend=False,
        stem_hydration=False,
    )
    with caplog.at_level(logging.INFO, logger=r2_capability.__name__), TestClient(app):
        records = [r for r in caplog.records if r.name == r2_capability.__name__]
    assert [r.levelno for r in records] == [logging.ERROR]
    assert "DEGRADED" in records[0].getMessage()
