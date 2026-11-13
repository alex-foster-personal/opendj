"""The hub checks the sha256 of lyric pair uploads and meters them like stems (#14394).

[if] a spoke can land wrong bytes under a digest key or upload unmetered [then] broken, [else stop].
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.cloud import asset_store
from apps.cloud.hub_lyrics_pair_client import LYRICS_PAIR_DIGEST_MISMATCH, LYRICS_PAIR_UPLOAD_PRESIGN_PATH
from apps.entitlements import Standing, StaticEntitlementSource
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import client as sync_client
from apps.sync_hub import entitlement_gate, stem_meter
from tests.cloudsync.conftest import (
    ENROLL_OWNER_EMAIL,
    ENROLL_OWNER_SUB,
    InMemoryAssetS3,
    _enroll_hub_app,
    seed_user,
)
from tests.cloudsync.enrollment_helpers import http_enroll, mint_grant
from tests.cloudsync.enrollment_transport import TestClientTransport

from .test_register_pair_hub_presign import _cfg

pytestmark = pytest.mark.requirement("LYRICS-14817")

_BODY = b"lyric-pair-bytes"
_DIGEST = hashlib.sha256(_BODY).hexdigest()


def _hub_dir(tmp_path: Path) -> Path:
    """A migrated hub DB with one signed-in owner, as ``enroll_hub_dir`` builds it."""
    path = tmp_path / "hub"
    path.mkdir(parents=True)
    db_path = sync_client.state_db_path(path)
    state_db.open_rw(db_path).close()
    seed_user(db_path, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    return path


@pytest.fixture
def hosted_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, Path]]:
    """A hosted hub whose owner holds active CloudStems, with one enrolled spoke."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")
    monkeypatch.setenv(stem_meter.CAP_ENV, "200")
    hub_dir = _hub_dir(tmp_path)
    app = _enroll_hub_app(hub_dir)
    app.state.sync_hub_hosted = True
    app.state.entitlement_source = StaticEntitlementSource(
        {(ENROLL_OWNER_SUB, entitlement_gate.CLOUDSTEMS_HOSTED_FEATURE): Standing("active", None)},
        provider="cloudstems-static",
    )
    with TestClient(app) as http:
        spoke_dir = tmp_path / "spoke"
        spoke_dir.mkdir()
        http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(hub_dir))
        yield http, machine_identity.get_or_create_machine_id(spoke_dir), hub_dir


def _usage_rows(hub_dir: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(sync_client.state_db_path(hub_dir))
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (stem_meter.USAGE_TABLE,)).fetchone():
            return []
        return conn.execute(f"SELECT owner_sub, stable_id FROM {stem_meter.USAGE_TABLE}").fetchall()
    finally:
        conn.close()


def _presign(http: TestClient, machine_id: str, stable_id: str = "sid-pair") -> httpx.Response:
    return http.post(
        LYRICS_PAIR_UPLOAD_PRESIGN_PATH,
        json={
            "machine_id": machine_id,
            "stable_id": stable_id,
            "files": [{"content_hash": _DIGEST, "size_bytes": len(_BODY)}],
        },
    )


def test_hub_refuses_and_deletes_same_size_wrong_bytes_under_a_digest(
    hosted_hub: tuple[TestClient, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] wrong bytes of the right size sit under a digest [then] the hub answers 409, [else stop]."""
    http, machine_id, _hub = hosted_hub
    cfg, s3 = _cfg(), InMemoryAssetS3()
    key = asset_store.asset_object_key(_DIGEST)
    s3.put_object_if_none_match(cfg.audio_bucket, key, bytes(reversed(_BODY)))
    monkeypatch.setattr("apps.sync_hub.service_lyrics_pair_assets._hub_r2_clients", lambda: (cfg, s3))
    response = _presign(http, machine_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == LYRICS_PAIR_DIGEST_MISMATCH
    assert s3.head_object(cfg.audio_bucket, key) is None
    retry = _presign(http, machine_id)
    assert retry.status_code == 200, retry.text
    assert retry.json()["files"][0]["present"] is False


def test_hub_meters_a_lyric_pair_upload_once_per_track(
    hosted_hub: tuple[TestClient, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a hosted hub presigns a lyric pair upload [then] it charges one stem track, [else stop]."""
    http, machine_id, hub_dir = hosted_hub
    cfg, s3 = _cfg(), InMemoryAssetS3()
    monkeypatch.setattr("apps.sync_hub.service_lyrics_pair_assets._hub_r2_clients", lambda: (cfg, s3))
    first = _presign(http, machine_id)
    assert first.status_code == 200, first.text
    assert _usage_rows(hub_dir) == [(ENROLL_OWNER_SUB, "sid-pair")]
    s3.put_object_if_none_match(cfg.audio_bucket, asset_store.asset_object_key(_DIGEST), _BODY)
    confirm = _presign(http, machine_id)
    assert confirm.json()["files"][0]["present"] is True
    assert _usage_rows(hub_dir) == [(ENROLL_OWNER_SUB, "sid-pair")]
    monkeypatch.setenv(stem_meter.CAP_ENV, "1")
    spent = _presign(http, machine_id, stable_id="sid-other")
    assert spent.status_code == 429, spent.text
    assert spent.json()["detail"]["code"] == stem_meter.CAP_EXCEEDED_CODE
    month = stem_meter.month_of(datetime.now(UTC))
    conn = sqlite3.connect(sync_client.state_db_path(hub_dir))
    try:
        assert stem_meter.used(conn, owner_sub=ENROLL_OWNER_SUB, month=month) == 1
    finally:
        conn.close()
