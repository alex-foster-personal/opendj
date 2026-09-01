"""R2 content-addressed asset tier: keys, hashing, push/fetch, presigning.

Contract under test: ``specs/design_decision_06.md`` over the v6 policy
tables of ``specs/design_decision_05.md``. The policy-resolution matrix, LRU
eviction and hydration write-path tests live in
:mod:`tests.cloudsync.test_asset_tier_policy` and
:mod:`tests.cloudsync.test_asset_tier_writes` (round 4 quality-gate ratchet:
this file crossed 1070 lines). The seed helpers below are shared by all
three -- imported, never duplicated, so they cannot drift on what a seeded
track/machine/policy/pin looks like. Acceptance criteria, one assertion block
each:

- if an object key is not ``assets/<sha256[:2]>/<sha256>``, or a non-SHA-256
  string mints a key at all, two producers hashing the same bytes write to
  different places and content addressing buys nothing -- broken.
- if pushing bytes already in the bucket re-sends the body, or a fetch
  accepts a body whose digest is not the key, the tier is not
  content-addressed -- broken.
- if a presigned URL omits a SigV4 field, carries an expiry over 900s, or
  two mints for the same digest are byte-identical, the "cache by
  content_hash NEVER by URL" rule has no teeth -- broken.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig, MissingEnvError

from .conftest import InMemoryAssetS3

pytestmark = pytest.mark.requirement("CAT-04")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# --- helpers -------------------------------------------------------------


def _write(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _seed_track(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    content_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?)",
        (stable_id, f"track {stable_id}", content_hash, "t0", "t0"),
    )


def _seed_machine(conn: sqlite3.Connection, machine_id: str) -> None:
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, data_root, "
        "first_seen, last_seen) VALUES (?, ?, 'macos', 0, '/tmp', 't0', 't0')",
        (machine_id, f"name-{machine_id}"),
    )


def _seed_policy(
    conn: sqlite3.Connection, machine_id: str, mode: str, *, asset_kind: str = "audio"
) -> None:
    conn.execute(
        "INSERT INTO sync_policies(machine_id, asset_kind, mode, "
        "cache_budget_mb, updated_at) VALUES (?, ?, ?, 100, 't0')",
        (machine_id, asset_kind, mode),
    )


def _seed_playlist_pin(
    conn: sqlite3.Connection,
    *,
    machine_id: str,
    playlist_id: str,
    stable_id: str,
    mode: str,
    position: int = 0,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO playlists(playlist_id, name, vendor, "
        "vendor_pl_id, created_at, updated_at) VALUES (?, ?, 'rekordbox', ?, "
        "'t0', 't0')",
        (playlist_id, f"pl-{playlist_id}", playlist_id),
    )
    conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) "
        "VALUES (?, ?, ?)",
        (playlist_id, stable_id, position),
    )
    conn.execute(
        "INSERT INTO playlist_pins(machine_id, playlist_id, mode, updated_at) "
        "VALUES (?, ?, ?, 't0')",
        (machine_id, playlist_id, mode),
    )


def _seed_local_location(
    conn: sqlite3.Connection,
    stable_id: str,
    file_path: Path,
    *,
    available: int = 1,
    content_hash: str | None = None,
    machine_id: str = "m1",
) -> None:
    """A local copy belonging to ONE machine (ADR 08 point 1).

    ``machine_id`` is not optional in practice even though the DDL allows
    NULL: every read and every write in the location path filters on it, so a
    row without one is invisible to the machine it describes -- which is
    exactly round 2 finding N1b.
    """
    conn.execute(
        "INSERT INTO track_locations(stable_id, machine_id, kind, role, "
        "file_path, available, content_hash, created_at, updated_at) "
        "VALUES (?, ?, 'local', 'primary', ?, ?, ?, 't0', 't0')",
        (stable_id, machine_id, str(file_path), available, content_hash),
    )


# --- object keys + hashing ----------------------------------------------


def test_asset_object_key_is_content_addressed_and_sharded():
    digest = _sha(b"stem bundle bytes")
    key = asset_store.asset_object_key(digest)
    assert key == f"assets/{digest[:2]}/{digest}"
    # The shard is derived, never independent: same digest, same key, always.
    assert asset_store.asset_object_key(digest) == key


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        _sha(b"x").upper(),  # uppercase hex is a different string on S3
        _sha(b"x")[:-1],  # truncated
        _sha(b"x")[:-1] + "z",  # non-hex char
    ],
)
def test_asset_object_key_refuses_anything_that_is_not_a_sha256(bad: str):
    with pytest.raises(asset_store.AssetStoreError):
        asset_store.asset_object_key(bad)


def test_compute_asset_hash_matches_hashlib_and_streams(tmp_path: Path):
    body = b"a" * (3 * (1 << 20) + 17)  # spans several read chunks
    path = _write(tmp_path / "big.flac", body)
    assert asset_store.compute_asset_hash(path) == _sha(body)


def test_compute_asset_hash_fails_loudly_on_a_missing_file(tmp_path: Path):
    with pytest.raises(asset_store.AssetStoreError):
        asset_store.compute_asset_hash(tmp_path / "nope.flac")


# --- credentials ---------------------------------------------------------


def test_missing_credentials_name_the_exact_doppler_secret(cfg: CloudConfig):
    blank = CloudConfig(
        r2_account_id=cfg.r2_account_id,
        r2_access_key_id="",
        r2_secret_access_key="  ",
        state_bucket=cfg.state_bucket,
        audio_bucket=cfg.audio_bucket,
        hostname=cfg.hostname,
        bind_host=cfg.bind_host,
    )
    with pytest.raises(MissingEnvError) as excinfo:
        asset_store.require_credentials(blank)
    message = str(excinfo.value)
    assert "R2_ACCESS_KEY_ID" in message
    assert "R2_SECRET_ACCESS_KEY" in message
    assert "R2_ACCOUNT_ID" not in message  # that one WAS present
    assert "doppler run" in message


# --- push / fetch / exists ----------------------------------------------


def test_push_asset_uploads_once_then_short_circuits(
    tmp_path: Path, cfg: CloudConfig, fake_s3: InMemoryAssetS3
):
    body = b"four stem bundle"
    path = _write(tmp_path / "bundle.zip", body)

    first = asset_store.push_asset(cfg, fake_s3, path)
    assert first.uploaded is True
    assert first.content_hash == _sha(body)
    assert first.object_key == f"assets/{_sha(body)[:2]}/{_sha(body)}"
    assert first.bucket == "test-audio"
    assert first.size_bytes == len(body)

    second = asset_store.push_asset(cfg, fake_s3, path)
    assert second.uploaded is False
    assert second.object_key == first.object_key
    # The body was sent exactly once; the second call was a HEAD only.
    assert len(fake_s3.put_calls) == 1


def test_push_asset_raises_when_the_upload_is_rejected_and_key_absent(
    tmp_path: Path, cfg: CloudConfig
):
    s3 = InMemoryAssetS3(fail_puts=True)
    path = _write(tmp_path / "bundle.zip", b"bytes")
    with pytest.raises(asset_store.AssetStoreError):
        asset_store.push_asset(cfg, s3, path)


def test_object_exists_tracks_the_bucket(
    tmp_path: Path, cfg: CloudConfig, fake_s3: InMemoryAssetS3
):
    body = b"evidence clip"
    digest = _sha(body)
    assert asset_store.object_exists(cfg, fake_s3, digest) is False
    asset_store.push_asset(cfg, fake_s3, _write(tmp_path / "clip.wav", body))
    assert asset_store.object_exists(cfg, fake_s3, digest) is True


def test_fetch_asset_round_trips_and_verifies_the_digest(
    tmp_path: Path, cfg: CloudConfig, fake_s3: InMemoryAssetS3
):
    body = b"hq audio copy"
    asset_store.push_asset(cfg, fake_s3, _write(tmp_path / "src.flac", body))
    dest = tmp_path / "out" / "fetched.flac"
    got = asset_store.fetch_asset(cfg, fake_s3, _sha(body), dest)
    assert got == dest
    assert dest.read_bytes() == body


def test_fetch_asset_rejects_a_body_that_does_not_match_its_key(
    tmp_path: Path, cfg: CloudConfig, fake_s3: InMemoryAssetS3
):
    """A corrupted object must never land in the cache as a valid entry."""
    body = b"hq audio copy"
    digest = _sha(body)
    key = asset_store.asset_object_key(digest)
    fake_s3.store[("test-audio", key)] = (b"truncated", '"etag"')
    with pytest.raises(asset_store.AssetStoreError):
        asset_store.fetch_asset(cfg, fake_s3, digest, tmp_path / "out.flac")
    assert not (tmp_path / "out.flac").exists()


def test_fetch_asset_fails_loudly_when_the_object_is_absent(
    tmp_path: Path, cfg: CloudConfig, fake_s3: InMemoryAssetS3
):
    with pytest.raises(asset_store.AssetStoreError):
        asset_store.fetch_asset(cfg, fake_s3, _sha(b"nothing"), tmp_path / "x")


# --- presigning ----------------------------------------------------------


def test_sigv4_signer_reproduces_the_aws_published_presign_vector():
    """The one test here that is not self-referential.

    Inputs and expected signature are AWS's own worked example for
    "Signature Calculation: Transfer Payload in a Single Chunk / query
    parameters" (GET ``examplebucket/test.txt``, 20130524T000000Z, 24h
    expiry, UNSIGNED-PAYLOAD). A signer checked only against its own output
    can be wrong in a way every other assertion in this file agrees with;
    this one anchors it to the published algorithm.
    """
    url = asset_store.sigv4_presigned_url(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        canonical_uri="/test.txt",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        expiry_seconds=86400,
        amz_date="20130524T000000Z",
        region="us-east-1",
    )
    signature = parse_qs(urlparse(url).query)["X-Amz-Signature"][0]
    assert signature == (
        "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )


def test_presign_url_shape_carries_every_sigv4_field(cfg: CloudConfig):
    digest = _sha(b"presign me")
    url = asset_store.presign_url(cfg, digest, 900, now=FIXED_NOW)
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "acct1234.r2.cloudflarestorage.com"
    assert parsed.path == f"/test-audio/assets/{digest[:2]}/{digest}"

    query = parse_qs(parsed.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Date"] == ["20260831T120000Z"]
    assert query["X-Amz-Credential"] == [
        "AKIDEXAMPLE/20260831/auto/s3/aws4_request"
    ]
    assert len(query["X-Amz-Signature"][0]) == 64


def test_presign_url_is_deterministic_for_a_fixed_clock(cfg: CloudConfig):
    digest = _sha(b"presign me")
    a = asset_store.presign_url(cfg, digest, 600, now=FIXED_NOW)
    b = asset_store.presign_url(cfg, digest, 600, now=FIXED_NOW)
    assert a == b


def test_presign_url_signature_differs_per_mint(cfg: CloudConfig):
    """Why the cache is keyed by content_hash and never by URL (ADR 06.4)."""
    digest = _sha(b"presign me")
    later = FIXED_NOW.replace(second=1)
    assert asset_store.presign_url(
        cfg, digest, 600, now=FIXED_NOW
    ) != asset_store.presign_url(cfg, digest, 600, now=later)


def test_presign_url_signature_differs_per_method(cfg: CloudConfig):
    digest = _sha(b"presign me")
    get_url = asset_store.presign_url(cfg, digest, 600, method="GET", now=FIXED_NOW)
    put_url = asset_store.presign_url(cfg, digest, 600, method="PUT", now=FIXED_NOW)
    assert get_url != put_url


@pytest.mark.parametrize("expiry", [0, -1, 901, 86400])
def test_presign_url_enforces_the_900_second_ceiling(cfg: CloudConfig, expiry: int):
    with pytest.raises(asset_store.AssetStoreError):
        asset_store.presign_url(cfg, _sha(b"x"), expiry, now=FIXED_NOW)


