"""Feedback screenshot R2 asset-tier push (issue #11235, FBSYNC-13618)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.r2_keys import asset_object_key
from apps.webui.server.routes import feedback_attachments
from tests.cloudsync.conftest import InMemoryAssetS3


@pytest.fixture(autouse=True)
def _allow_feedback_attachment_r2_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(feedback_attachments.FEEDBACK_ATTACHMENT_R2_PUSH_ENV, "1")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app, base_url="http://127.0.0.1")


def _png(color: tuple[int, int, int] = (1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _create_pin(client: TestClient) -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 1,
            "y_pct": 2,
            "anchor": ".a",
            "page": "/performance",
            "text": "screenshot",
            "ui": "chrome-loop",
            "viewport_width": 800,
            "viewport_height": 600,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _wire_r2_push(
    monkeypatch: pytest.MonkeyPatch,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
) -> None:
    monkeypatch.setattr(feedback_attachments, "_cloud_config_for_attachment_push", lambda: cfg)
    monkeypatch.setattr(feedback_attachments, "_asset_client_for_upload", lambda _cfg: fake_s3)


@pytest.mark.requirement("FBSYNC-13618")
def test_upload_with_r2_credentials_stores_bytes_at_canonical_asset_key(
    client: TestClient,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] R2 is configured on upload [then] bytes land at assets/<h[:2]>/<h>, [else stop]."""
    _wire_r2_push(monkeypatch, cfg, fake_s3)
    png = _png()
    digest = hashlib.sha256(png).hexdigest()
    key = asset_object_key(digest)
    assert key == f"assets/{digest[:2]}/{digest}"

    pin = _create_pin(client)
    uploaded = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("shot.png", png, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert (cfg.audio_bucket, key) in fake_s3.store
    stored_body, _etag = fake_s3.store[(cfg.audio_bucket, key)]
    assert stored_body == png


@pytest.mark.requirement("FBSYNC-13618")
def test_same_attachment_bytes_uploaded_again_are_idempotent(
    client: TestClient,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] identical screenshot bytes upload twice with R2 [then] only one PUT runs, [else stop]."""
    _wire_r2_push(monkeypatch, cfg, fake_s3)
    png = _png()
    pin_a = _create_pin(client)
    pin_b = _create_pin(client)
    first = client.post(
        f"/api/v1/feedback/comments/{pin_a['id']}/attachment",
        files={"file": ("a.png", png, "image/png")},
    )
    second = client.post(
        f"/api/v1/feedback/comments/{pin_b['id']}/attachment",
        files={"file": ("b.png", png, "image/png")},
    )
    assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
    assert len(fake_s3.put_calls) == 1


@pytest.mark.requirement("FBSYNC-13618")
def test_missing_r2_credentials_still_keeps_local_attachment_file(
    client: TestClient,
    tmp_path: Path,
    fake_s3: InMemoryAssetS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] R2 credentials are absent [then] upload keeps the local file only, [else stop]."""

    def _missing() -> CloudConfig:
        raise MissingEnvError("Missing required env vars: R2_ACCOUNT_ID")

    monkeypatch.setattr(feedback_attachments, "_cloud_config_for_attachment_push", _missing)
    monkeypatch.setattr(feedback_attachments, "_asset_client_for_upload", lambda _cfg: fake_s3)
    png = _png()
    pin = _create_pin(client)
    uploaded = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("shot.png", png, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    attachment_id = uploaded.json()["attachment"]["id"]
    local_path = tmp_path / "feedback" / "attachments" / f"{attachment_id}.png"
    assert local_path.is_file()
    assert local_path.read_bytes() == png
    assert not fake_s3.put_calls


@pytest.mark.requirement("FBSYNC-13618")
def test_push_uses_asset_object_key_layout_not_an_ad_hoc_prefix(
    client: TestClient,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the object key is not assets/<sha256[:2]>/<sha256> [then] the tier contract broke, [else stop]."""
    _wire_r2_push(monkeypatch, cfg, fake_s3)
    png = _png(color=(9, 8, 7))
    digest = hashlib.sha256(png).hexdigest()
    expected = asset_store.asset_object_key(digest)
    pin = _create_pin(client)
    client.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("shot.png", png, "image/png")},
    )
    keys = [key for bucket, key in fake_s3.store if bucket == cfg.audio_bucket]
    assert keys == [expected]
