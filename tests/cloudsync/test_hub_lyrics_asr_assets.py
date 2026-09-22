"""Hub presigned ASR lyric transcript endpoints (LYRICS-07, #2851).

[if] hub ASR presign or fetch endpoints mis-key or leak transcripts [then] fail, [else stop].
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.cloud.config import CloudConfig
from apps.cloud.lyrics_asr_source import (
    LYRICS_ASR_NOT_FOUND,
    lyrics_asr_object_key,
)
from tests.cloudsync.conftest import InMemoryAssetS3, _enroll_hub_app
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.presign_asset_rig import serve_presigned_assets_by_key

pytestmark = pytest.mark.requirement("LYRICS-07")


@pytest.fixture(autouse=True)
def _hub_observe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")


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


def _transcript_bytes(stable_id: str) -> bytes:
    payload = {
        "schema_version": 1,
        "stable_id": stable_id,
        "vocals_sha256": "a" * 64,
        "model": "large-v3",
        "generated_at": "2026-09-15T12:00:00Z",
        "language": "en",
        "language_probability": 0.99,
        "duration_s": 3.0,
        "text": "hello world",
        "words": [
            {"word": "hello", "start_s": 0.5, "end_s": 1.0, "prob": 0.9},
            {"word": "world", "start_s": 1.1, "end_s": 1.6, "prob": 0.9},
        ],
    }
    return (json.dumps(payload) + "\n").encode("utf-8")


def test_hub_presign_returns_url_for_existing_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "asr-track"
    body = _transcript_bytes(stable_id)
    object_key = lyrics_asr_object_key(stable_id)
    s3.put_object_if_none_match(cfg.audio_bucket, object_key, body)
    monkeypatch.setattr(
        "apps.sync_hub.service_lyrics_asr_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )
    from apps.sync_hub import client

    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        with serve_presigned_assets_by_key(cfg, {object_key: body}):
            response = hub_http.get(
                f"/api/v1/sync/lyrics-asr/{stable_id}",
                params={"machine_id": machine_id},
            )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stable_id"] == stable_id
    assert payload["content_hash"] == hashlib.sha256(body).hexdigest()
    assert payload["url"].startswith("http://127.0.0.1")


def test_hub_presign_missing_transcript_is_named_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "missing-asr"
    monkeypatch.setattr(
        "apps.sync_hub.service_lyrics_asr_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )
    from apps.sync_hub import client

    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        response = hub_http.get(
            f"/api/v1/sync/lyrics-asr/{stable_id}",
            params={"machine_id": machine_id},
        )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == LYRICS_ASR_NOT_FOUND


def test_hub_presign_response_has_no_r2_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_dir = tmp_path / "hub"
    spoke_dir = tmp_path / "spoke"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    stable_id = "cred-check-asr"
    body = _transcript_bytes(stable_id)
    object_key = lyrics_asr_object_key(stable_id)
    s3.put_object_if_none_match(cfg.audio_bucket, object_key, body)
    monkeypatch.setattr(
        "apps.sync_hub.service_lyrics_asr_assets._hub_r2_clients",
        lambda: (cfg, s3),
    )
    from apps.sync_hub import client

    with TestClient(_enroll_hub_app(hub_dir)) as hub_http:
        transport = TestClientTransport(hub_http)
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        response = hub_http.get(
            f"/api/v1/sync/lyrics-asr/{stable_id}",
            params={"machine_id": machine_id},
        )
    assert response.status_code == 200
    text = response.text
    assert "R2_ACCESS_KEY_ID" not in text
    assert cfg.r2_secret_access_key not in text
    assert response.json()["url"].startswith("https://")
