"""stems_sync: push/hydrate without moving local files or writing track_locations."""

from __future__ import annotations

import hashlib
import json
import math
import wave
from pathlib import Path

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.lyrics import register_stems, stems_sync
from apps.shared.state import db as state_db
from apps.stems.artifacts import load_stem_bundle

from .conftest import (
    InMemoryAssetS3,
    seed_stamped_policy,
    seed_track,
    use_cloud_mode,
    use_local_mode,
)


def _test_cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct1234",
        r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="test-host",
        bind_host="127.0.0.1",
    )


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    frames = int(seconds * 8000)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(
            b"".join(
                int(3000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) * 2
                for i in range(frames)
            )
        )


def test_push_missing_dry_run_lists_work_without_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_local_mode(monkeypatch)
    data_dir = tmp_path / "data"
    external = tmp_path / "external-stems"
    external.mkdir()
    monkeypatch.setenv("MDT_EXTERNAL_STEM_ROOTS", str(external))
    stems_root = data_dir / "state" / "stems-roformer-spike" / "sid1"
    stems_root.mkdir(parents=True)
    _write_wav(stems_root / "vocals.wav")
    _write_wav(stems_root / "instrumental.wav")
    (stems_root / "manifest.json").write_text(
        json.dumps({
            "schema_version": 3,
            "stable_id": "sid1",
            "layout": "roformer2",
            "model": {"name": "m", "version": "v"},
            "source": {"path": "/x/a.wav", "sha256": "0" * 64},
            "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            "audio": {"sample_rate": 8000, "channels": 2, "frame_count": 8000},
        }),
        encoding="utf-8",
    )
    rc = stems_sync.push_missing(data_dir=data_dir, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "local:" in out
    assert "dry run: no credentials used, nothing uploaded" in out


def _seed_s3_object(fake_s3: InMemoryAssetS3, cfg: CloudConfig, path: Path) -> str:
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    key = asset_store.asset_object_key(digest)
    fake_s3.store[(cfg.audio_bucket, key)] = (body, fake_s3._etag(body))
    return digest


def test_hydrate_fetches_by_hash_and_passes_strict_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_local_mode(monkeypatch)
    cfg = _test_cfg()
    data_dir = tmp_path / "data"
    stems_root = data_dir / "state" / "stems-roformer-spike"
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_wav(source_root / "v.wav", 1.0)
    _write_wav(source_root / "i.wav", 1.0)
    bundle_dir = register_stems.register_pair(
        stable_id="sid-hydrate",
        vocals=source_root / "v.wav",
        instrumental=source_root / "i.wav",
        model_name="m",
        model_version="v",
        source_path=str(source_root / "v.wav"),
        storage=register_stems.RegisterPairStorage(root=tmp_path / "unused-root"),
    )
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    external_manifest = tmp_path / "manifest.json"
    external_manifest.write_text(
        (bundle_dir / "manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for _part, rel in manifest["files"].items():
        _seed_s3_object(fake_s3, cfg, bundle_dir / rel)

    use_cloud_mode(monkeypatch)
    monkeypatch.setattr(
        "apps.lyrics.stems_sync.asset_clients_for_mode",
        lambda writing=True: (fake_s3, cfg),
    )
    dest_root = stems_root
    assert not (dest_root / "sid-hydrate").exists()
    rc = stems_sync.hydrate(
        "sid-hydrate",
        manifest_path=external_manifest,
        data_dir=data_dir,
        dry_run=False,
    )
    assert rc == 0
    bundle = load_stem_bundle("sid-hydrate", stems_dir=dest_root)
    assert set(bundle.files) == {"vocals", "instrumental"}


def test_hydrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({
            "schema_version": 3,
            "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            "files_sha256": {"vocals": "a" * 64, "instrumental": "b" * 64},
        }),
        encoding="utf-8",
    )
    rc = stems_sync.hydrate(
        "sid1",
        manifest_path=manifest,
        data_dir=tmp_path / "data",
        dry_run=True,
    )
    assert rc == 0
    assert not (tmp_path / "data" / "state" / "stems-roformer-spike" / "sid1").exists()


def test_register_cloud_push_verifies_without_track_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_cloud_mode(monkeypatch)
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-cloud")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="pinned")
    finally:
        conn.close()

    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins)
    root = data_dir / "state" / "stems-roformer-spike"

    cfg = _test_cfg()
    register_stems.register_pair(
        stable_id="sid-cloud",
        vocals=voc,
        instrumental=ins,
        model_name="m",
        model_version="v",
        source_path=str(voc),
        storage=register_stems.RegisterPairStorage(
            root=root, s3=fake_s3, cfg=cfg, conn=state_db.open_rw(db_path)
        ),
    )
    conn = state_db.open_rw(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM track_locations").fetchone()[0]
    finally:
        conn.close()
    assert rows == 0
    assert voc.is_file() and ins.is_file()
    assert len(fake_s3.put_calls) >= 2
    manifest = json.loads((root / "sid-cloud" / "manifest.json").read_text())
    for digest in manifest["files_sha256"].values():
        assert asset_store.object_exists(cfg, fake_s3, digest)
    manifest_digest = hashlib.sha256(
        (root / "sid-cloud" / "manifest.json").read_bytes()
    ).hexdigest()
    assert asset_store.object_exists(cfg, fake_s3, manifest_digest)


def test_register_cloud_excluded_pushes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_cloud_mode(monkeypatch)
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-excl")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="excluded")
    finally:
        conn.close()

    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins)
    root = data_dir / "state" / "stems-roformer-spike"
    register_stems.register_pair(
        stable_id="sid-excl",
        vocals=voc,
        instrumental=ins,
        model_name="m",
        model_version="v",
        source_path=str(voc),
        storage=register_stems.RegisterPairStorage(
            root=root, s3=fake_s3, cfg=_test_cfg(), conn=state_db.open_rw(db_path)
        ),
    )
    assert fake_s3.put_calls == []


def test_register_cloud_stream_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_cloud_mode(monkeypatch)
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-stream")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="stream")
    finally:
        conn.close()

    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins)
    with pytest.raises(HydrationError, match="stream"):
        register_stems.register_pair(
            stable_id="sid-stream",
            vocals=voc,
            instrumental=ins,
            model_name="m",
            model_version="v",
            source_path=str(voc),
            storage=register_stems.RegisterPairStorage(
                root=data_dir / "state" / "stems-roformer-spike",
                s3=fake_s3,
                cfg=_test_cfg(),
                conn=state_db.open_rw(db_path),
            ),
        )
