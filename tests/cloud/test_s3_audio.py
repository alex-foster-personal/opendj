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


@pytest.mark.requirement("CAT-04")
def test_fake_s3_put_if_match_wildcard_existing_object():
    """Regression: adv-r4 P11-R4-02.

    FakeS3Client treated ``etag='*'`` as a literal string compare against
    the stored sha1 etag, so a PUT with If-Match ``*`` against an
    existing key always returned ``(False, None)``. In real S3, ``*`` is
    the wildcard meaning "match any existing object". Assert the fake
    now matches production semantics.
    """
    s3 = FakeS3Client()

    # Non-existent key: wildcard must NOT match (no object to match).
    ok, etag = s3.put_object_if_match("bucket", "absent", b"v1", etag="*")
    assert ok is False
    assert etag is None

    # Create the object.
    created, created_etag = s3.put_object_if_none_match(
        "bucket", "existing", b"v1"
    )
    assert created is True
    assert created_etag is not None

    # Existing key: wildcard must succeed and return a new etag.
    ok2, new_etag = s3.put_object_if_match("bucket", "existing", b"v2", etag="*")
    assert ok2 is True
    assert new_etag is not None
    assert new_etag != created_etag

    # Body was overwritten.
    got = s3.get_object("bucket", "existing")
    assert got is not None
    assert got[0] == b"v2"


@pytest.mark.requirement("CAT-04")
def test_fake_s3_put_if_match_strict_etag_still_works():
    """Strict etag CAS must still mismatch when the stored etag differs."""
    s3 = FakeS3Client()
    s3.put_object_if_none_match("bucket", "k", b"v1")
    ok, _ = s3.put_object_if_match(
        "bucket", "k", b"v2", etag='"deadbeef"'
    )
    assert ok is False


@pytest.mark.requirement("CAT-04")
def test_upload_audio_is_idempotent_for_existing_object(tmp_path: Path):
    """Re-uploading the same stable_id must succeed, not raise."""
    s3 = FakeS3Client()
    cfg = make_cfg()
    p = tmp_path / "song.mp3"
    p.write_bytes(b"mp3 frame data")
    key1 = upload_audio(cfg, s3, p, stable_id="deadbeef" + "0" * 32)

    # Second upload of the same stable_id would previously raise
    # AudioUploadError because the fake treated If-Match "*" as a literal
    # compare and the fallback put_object_if_none_match returned
    # (False, None). With the wildcard fix the first branch succeeds.
    key2 = upload_audio(cfg, s3, p, stable_id="deadbeef" + "0" * 32)
    assert key1 == key2
