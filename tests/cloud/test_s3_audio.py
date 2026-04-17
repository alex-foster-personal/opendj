"""Tests for apps.cloud.s3_audio (CAT-04b opt-in helpers)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.lock import FakeS3Client
from apps.cloud.s3_audio import (
    AudioUploadError,
    audio_object_key,
    compute_content_hash,
    upload_audio,
)


def make_cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct",
        r2_access_key_id="id",
        r2_secret_access_key="secret",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="host",
        bind_host="127.0.0.1",
    )


@pytest.mark.requirement("CAT-04")
def test_audio_object_key_shards_by_prefix():
    key = audio_object_key("abcd1234ef" * 4, ".mp3")
    assert key == "audio/ab/" + "abcd1234ef" * 4 + ".mp3"
    # Extension missing dot gets one.
    key2 = audio_object_key("ffff", "m4a")
    assert key2 == "audio/ff/ffff.m4a"


@pytest.mark.requirement("CAT-04")
def test_compute_content_hash_matches_reference(tmp_path: Path):
    data = b"test audio bytes" * 100
    p = tmp_path / "t.mp3"
    p.write_bytes(data)
    got = compute_content_hash(p)
    assert got == hashlib.sha256(data).hexdigest()


@pytest.mark.requirement("CAT-04")
def test_upload_audio_creates_object(tmp_path: Path):
    s3 = FakeS3Client()
    cfg = make_cfg()
    p = tmp_path / "song.mp3"
    p.write_bytes(b"mp3 frame data")
    key = upload_audio(cfg, s3, p, stable_id="deadbeef" + "0" * 32)
    assert key.startswith("audio/de/deadbeef")
    assert s3.get_object("test-audio", key) is not None


@pytest.mark.requirement("CAT-04")
def test_upload_audio_raises_on_missing_file(tmp_path: Path):
    s3 = FakeS3Client()
    cfg = make_cfg()
    with pytest.raises(AudioUploadError):
        upload_audio(cfg, s3, tmp_path / "nope.mp3", stable_id="abc" + "0" * 37)
