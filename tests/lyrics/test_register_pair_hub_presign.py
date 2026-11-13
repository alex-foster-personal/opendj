"""Lyric stem pair registration through hub-presigned PUTs (LYRICS-13411, #13411).

[if] a spoke without R2 credentials registers a pair [then] it uses hub PUTs or fails closed, [else stop].
"""

from __future__ import annotations

import math
import threading
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from apps.cloud import asset_store
from apps.cloud.asset_store import PRESIGN_HOST_ENV, PRESIGN_SCHEME_ENV
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.cloud.hub_lyrics_pair_client import (
    LYRICS_PAIR_DIGEST_MISMATCH,
    LYRICS_PAIR_HUB_UNAVAILABLE,
    LYRICS_PAIR_PRESIGN_FAILED,
    LYRICS_PAIR_UPLOAD_PRESIGN_PATH,
)
from apps.lyrics import batch
from apps.lyrics import register_stems as rs
from apps.lyrics.batch import StageBlocked
from apps.shared.state import db as state_db
from tests.cloudsync.conftest import InMemoryAssetS3, _enroll_hub_app
from tests.cloudsync.enrollment_transport import TestClientTransport

from .conftest import seed_stamped_policy, seed_track, use_cloud_mode

pytestmark = pytest.mark.requirement("LYRICS-13411")

_R2_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def _cfg() -> CloudConfig:
    """Hub-side R2 config; the spoke under test never sees it."""
    return CloudConfig(
        r2_account_id="acct",
        r2_access_key_id="hub-key-id",
        r2_secret_access_key="hub-secret-value",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="host",
        bind_host="127.0.0.1",
    )


def _write_wav(path: Path, seconds: float = 0.5) -> None:
    """Write a short stereo sine WAV that ffprobe reads."""
    frames = int(seconds * 8000)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(
            b"".join(int(3000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) * 2 for i in range(frames))
        )


@contextmanager
def _serve_presigned_puts(cfg: CloudConfig, s3: InMemoryAssetS3) -> Iterator[list[str]]:
    """Loopback R2 stand-in: presigned PUTs land in ``s3``; yields the PUT key log."""
    import os

    puts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            """Store the body under the bucket key the presigned path names."""
            path = unquote(self.path.split("?", 1)[0])
            prefix = f"/{cfg.audio_bucket}/"
            if not path.startswith(prefix) or "X-Amz-Signature=" not in self.path:
                self.send_error(403)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            key = path[len(prefix) :]
            s3.put_object_if_none_match(cfg.audio_bucket, key, body)
            puts.append(key)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: Any) -> None:
            """Keep test output quiet."""
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous = {name: os.environ.get(name) for name in (PRESIGN_HOST_ENV, PRESIGN_SCHEME_ENV)}
    os.environ[PRESIGN_HOST_ENV] = f"127.0.0.1:{server.server_address[1]}"
    os.environ[PRESIGN_SCHEME_ENV] = "http"
    try:
        yield puts
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)


@pytest.fixture
def spoke_without_r2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cloud-mode spoke with a pinned stem policy and no R2 env; returns its state.db."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")
    for var in _R2_VARS:
        monkeypatch.delenv(var, raising=False)
    db_path = tmp_path / "data" / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-hub")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="pinned")
    finally:
        conn.close()
    return db_path


def _register(tmp_path: Path, db_path: Path) -> Path:
    """Register one WAV pair with no explicit S3 client or hub, as the batch does."""
    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins, seconds=0.45)
    return rs.register_pair(
        stable_id="sid-hub",
        vocals=voc,
        instrumental=ins,
        model_name="m",
        model_version="v",
        source_path=str(voc),
        storage=rs.RegisterPairStorage(
            root=tmp_path / "data" / "state" / "stems-roformer-spike",
            conn=state_db.open_rw(db_path),
            data_dir=tmp_path / "spoke",
        ),
    )


def test_register_pair_without_r2_uploads_through_the_configured_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoke_without_r2: Path
) -> None:
    """[if] a spoke has a hub and no R2 credentials [then] the pair lands in R2 via hub PUTs, [else stop]."""
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    monkeypatch.setattr("apps.sync_hub.service_lyrics_pair_assets._hub_r2_clients", lambda: (cfg, s3))
    from apps.sync_hub import client

    with TestClient(_enroll_hub_app(tmp_path / "hub")) as hub_http:
        transport = TestClientTransport(hub_http)
        spoke_dir = tmp_path / "spoke"
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        seen: list[Path] = []

        def _target(data_dir: Path) -> rs.HubPairTarget:
            """Stand in for the configured hub of ``data_dir``."""
            seen.append(data_dir)
            return rs.HubPairTarget(transport=transport, machine_id=machine_id)

        monkeypatch.setattr(rs, "hub_pair_target", _target)
        use_cloud_mode(monkeypatch)
        with _serve_presigned_puts(cfg, s3) as puts:
            bundle = _register(tmp_path, spoke_without_r2)
    assert seen == [spoke_dir]
    assert len(puts) == 3
    digests = [
        asset_store.compute_asset_hash(bundle / name) for name in ("vocals.wav", "instrumental.wav", "manifest.json")
    ]
    for digest in digests:
        assert asset_store.object_exists(cfg, s3, digest)


def test_hub_presign_answers_present_without_a_url_and_carries_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a digest is already in R2 [then] the hub answers present with no URL and no secret, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    body = b"already-there"
    present = asset_store.compute_asset_hash(_bytes_file(tmp_path / "p.bin", body))
    s3.put_object_if_none_match(cfg.audio_bucket, asset_store.asset_object_key(present), body)
    absent = "b" * 64
    monkeypatch.setattr("apps.sync_hub.service_lyrics_pair_assets._hub_r2_clients", lambda: (cfg, s3))
    from apps.sync_hub import client

    with TestClient(_enroll_hub_app(tmp_path / "hub")) as hub_http:
        spoke_dir = tmp_path / "spoke"
        client.run_sync(spoke_dir, "http://hub.invalid", transport=TestClientTransport(hub_http), name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        response = hub_http.post(
            LYRICS_PAIR_UPLOAD_PRESIGN_PATH,
            json={
                "machine_id": machine_id,
                "stable_id": "sid-hub",
                "files": [
                    {"content_hash": present, "size_bytes": len(body)},
                    {"content_hash": absent, "size_bytes": 10},
                ],
            },
        )
        bad = hub_http.post(
            LYRICS_PAIR_UPLOAD_PRESIGN_PATH,
            json={"machine_id": machine_id, "stable_id": "../x", "files": [{"content_hash": absent, "size_bytes": 1}]},
        )
    assert response.status_code == 200
    rows = {row["content_hash"]: row for row in response.json()["files"]}
    assert rows[present] == {"content_hash": present, "present": True, "url": None}
    assert rows[absent]["present"] is False
    assert "X-Amz-Signature=" in rows[absent]["url"]
    assert cfg.r2_secret_access_key not in response.text
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == LYRICS_PAIR_PRESIGN_FAILED


def _bytes_file(path: Path, body: bytes) -> Path:
    """Write ``body`` to ``path`` and return the path."""
    path.write_bytes(body)
    return path


def test_register_pair_without_r2_or_hub_fails_closed_with_a_named_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoke_without_r2: Path
) -> None:
    """[if] no hub presign target is available [then] registration raises LYRICS_PAIR_HUB_UNAVAILABLE, [else stop]."""
    monkeypatch.setattr(rs, "hub_pair_target", lambda _data_dir: None)
    use_cloud_mode(monkeypatch)
    with pytest.raises(HydrationError, match=LYRICS_PAIR_HUB_UNAVAILABLE):
        _register(tmp_path, spoke_without_r2)


class _NeverLandsTransport:
    """A hub that presigns PUTs but whose HEAD never finds the upload."""

    def __init__(self) -> None:
        self.posts = 0

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Answer every digest absent with a PUT URL that accepts and discards the body."""
        self.posts += 1
        return {
            "stable_id": payload["stable_id"],
            "files": [
                {"content_hash": f["content_hash"], "present": False, "url": "http://unused.invalid/put"}
                for f in payload["files"]
            ],
        }

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Unused by pair registration."""
        raise AssertionError("pair registration must not GET")


def test_register_pair_refuses_when_the_hub_cannot_confirm_the_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoke_without_r2: Path
) -> None:
    """[if] the hub HEAD after upload misses a digest [then] registration raises LYRICS_PAIR_PRESIGN_FAILED, [else stop]."""
    transport = _NeverLandsTransport()
    monkeypatch.setattr(rs, "hub_pair_target", lambda _d: rs.HubPairTarget(transport=transport, machine_id="m1"))
    monkeypatch.setattr(rs, "put_presigned_file", lambda *_a, **_k: None)
    use_cloud_mode(monkeypatch)
    with pytest.raises(HydrationError, match=LYRICS_PAIR_PRESIGN_FAILED):
        _register(tmp_path, spoke_without_r2)
    assert transport.posts == 2


def test_batch_r2_preflight_passes_when_a_hub_target_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] R2 env is absent but a hub target exists [then] the batch preflight does not block, [else stop]."""
    for var in _R2_VARS:
        monkeypatch.delenv(var, raising=False)
    use_cloud_mode(monkeypatch)
    monkeypatch.setattr(
        batch, "hub_pair_target", lambda _d: rs.HubPairTarget(transport=_NeverLandsTransport(), machine_id="m1")
    )
    batch._preflight_r2_credentials(tmp_path)
    monkeypatch.setattr(batch, "hub_pair_target", lambda _d: None)
    with pytest.raises(StageBlocked, match="R2_ACCESS_KEY_ID"):
        batch._preflight_r2_credentials(tmp_path)


def test_register_pair_fails_when_the_presigned_put_lands_wrong_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoke_without_r2: Path
) -> None:
    """[if] a presigned PUT stores bytes that do not hash to the key [then] registration fails, [else stop]."""
    cfg, s3 = _cfg(), InMemoryAssetS3()
    monkeypatch.setattr("apps.sync_hub.service_lyrics_pair_assets._hub_r2_clients", lambda: (cfg, s3))
    real_put = s3.put_object_if_none_match

    def _corrupting_put(bucket: str, key: str, body: object) -> tuple[bool, str | None]:
        """Store the body reversed: same size, different digest."""
        assert isinstance(body, bytes)
        return real_put(bucket, key, bytes(reversed(body)))

    monkeypatch.setattr(s3, "put_object_if_none_match", _corrupting_put)
    from apps.sync_hub import client

    with TestClient(_enroll_hub_app(tmp_path / "hub")) as hub_http:
        transport = TestClientTransport(hub_http)
        spoke_dir = tmp_path / "spoke"
        client.run_sync(spoke_dir, "http://hub.invalid", transport=transport, name="spoke")
        machine_id = (spoke_dir / "machine-id").read_text(encoding="utf-8").strip()
        monkeypatch.setattr(
            rs, "hub_pair_target", lambda _d: rs.HubPairTarget(transport=transport, machine_id=machine_id)
        )
        use_cloud_mode(monkeypatch)
        with _serve_presigned_puts(cfg, s3), pytest.raises(HydrationError, match=LYRICS_PAIR_DIGEST_MISMATCH):
            _register(tmp_path, spoke_without_r2)
    assert s3.store == {}
