"""R2 content-addressed asset tier + policy hydration (ADR 06).

Contract under test: ``specs/design_decision_06.md`` over the v6 policy
tables of ``specs/design_decision_05.md``. Acceptance criteria, one
assertion block each:

- if an object key is not ``assets/<sha256[:2]>/<sha256>``, or a non-SHA-256
  string mints a key at all, two producers hashing the same bytes write to
  different places and content addressing buys nothing -- broken.
- if pushing bytes already in the bucket re-sends the body, or a fetch
  accepts a body whose digest is not the key, the tier is not
  content-addressed -- broken.
- if a presigned URL omits a SigV4 field, carries an expiry over 900s, or
  two mints for the same digest are byte-identical, the "cache by
  content_hash NEVER by URL" rule has no teeth -- broken.
- if the policy matrix resolves any of pinned/cached/stream/excluded, or a
  playlist-pin override, differently from ADR 06 point 4, a gig crate can
  silently end up streaming over venue wifi -- broken.
- if eviction leaves the cache over budget, evicts the most-recently-used
  first, or picks a different victim on a rerun, the LRU is not an LRU --
  broken.
- if push-then-delete removes a local file whose upload did not land, the
  one irreversible step in the write path is unguarded -- broken.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from apps.cloud import asset_store, hydration
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


# --- policy resolution matrix -------------------------------------------


@pytest.mark.parametrize(
    "mode,expected_origin",
    [
        ("pinned", "presigned"),
        ("cached", "presigned"),
        ("stream", "presigned"),
        ("excluded", "unavailable"),
    ],
)
def test_policy_matrix_with_no_local_copy_and_no_cache(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    tmp_path: Path,
    mode: str,
    expected_origin: str,
):
    """Only 'excluded' refuses to mint a URL; the rest fall through to R2."""
    digest = _sha(b"body")
    _seed_track(conn, "t1", content_hash=digest)
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", mode)

    source = hydration.resolve_playback_source(
        conn,
        "t1",
        "m1",
        asset_kind="audio",
        cache_dir=tmp_path / "cache",
        cfg=cfg,
    )
    assert source.origin == expected_origin
    assert source.mode == mode
    assert source.policy_source == "sync_policies"
    if expected_origin == "unavailable":
        assert source.reason is not None and "excluded" in source.reason
        assert source.url is None
    else:
        assert source.url is not None
        assert source.expires_in_seconds == 900
    # An unhydrated pin is flagged for the config UI, never silently ignored.
    assert source.pin_unhydrated is (mode == "pinned")


@pytest.mark.parametrize("mode", ["pinned", "cached", "stream"])
def test_a_present_local_file_always_wins(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path, mode: str
):
    """Mode governs what a machine KEEPS, not what it READS."""
    body = b"local body"
    audio = _write(tmp_path / "music" / "song.flac", body)
    _seed_track(conn, "t1", content_hash=_sha(body))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", mode)
    _seed_local_location(conn, "t1", audio, content_hash=_sha(body))

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache", cfg=cfg
    )
    assert source.origin == "local"
    assert source.path == audio
    assert source.pin_unhydrated is False


def test_excluded_short_circuits_even_with_a_local_file(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    audio = _write(tmp_path / "music" / "song.flac", b"local body")
    _seed_track(conn, "t1", content_hash=_sha(b"local body"))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "excluded")
    _seed_local_location(conn, "t1", audio)

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache", cfg=cfg
    )
    assert source.origin == "unavailable"


def test_a_stale_available_flag_does_not_hand_back_a_dead_path(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """``available=1`` is a probe stamp; the file is re-checked on disk."""
    digest = _sha(b"body")
    _seed_track(conn, "t1", content_hash=digest)
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached")
    _seed_local_location(conn, "t1", tmp_path / "music" / "evicted.flac")

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache", cfg=cfg
    )
    assert source.origin == "presigned"


def test_cache_hit_is_keyed_by_content_hash_and_bumps_atime(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    body = b"cached body"
    digest = _sha(body)
    cache_dir = tmp_path / "cache"
    cached = _write(hydration.cache_path(cache_dir, digest), body)
    os.utime(cached, (1_600_000_000, 1_600_000_000))

    _seed_track(conn, "t1", content_hash=digest)
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached")

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=cache_dir, cfg=cfg
    )
    assert source.origin == "cache"
    assert source.path == cached
    assert cached.stat().st_atime > 1_600_000_000
    # mtime is preserved so eviction ordering is not confused with writes.
    assert cached.stat().st_mtime == 1_600_000_000


@pytest.mark.parametrize(
    "default_mode,pin_mode",
    [
        ("stream", "pinned"),
        ("excluded", "pinned"),
        ("pinned", "excluded"),
        ("cached", "stream"),
    ],
)
def test_playlist_pin_overrides_the_asset_kind_default(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    tmp_path: Path,
    default_mode: str,
    pin_mode: str,
):
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", default_mode)
    _seed_playlist_pin(
        conn, machine_id="m1", playlist_id="p1", stable_id="t1", mode=pin_mode
    )

    resolution = hydration.resolve_policy(conn, "t1", "m1", asset_kind="audio")
    assert resolution.mode == pin_mode
    assert resolution.source == "playlist_pin"
    assert resolution.playlist_id == "p1"


def test_conflicting_pins_resolve_to_the_strongest_mode(
    conn: sqlite3.Connection, tmp_path: Path
):
    """A pin exists to guarantee availability, so the strongest claim wins."""
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")
    _seed_playlist_pin(
        conn, machine_id="m1", playlist_id="p-zzz", stable_id="t1", mode="pinned"
    )
    _seed_playlist_pin(
        conn, machine_id="m1", playlist_id="p-aaa", stable_id="t1", mode="excluded"
    )

    resolution = hydration.resolve_policy(conn, "t1", "m1", asset_kind="audio")
    assert resolution.mode == "pinned"
    assert resolution.playlist_id == "p-zzz"


def test_a_pin_for_another_machine_is_ignored(conn: sqlite3.Connection):
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    _seed_machine(conn, "m2")
    _seed_policy(conn, "m1", "stream")
    _seed_playlist_pin(
        conn, machine_id="m2", playlist_id="p1", stable_id="t1", mode="pinned"
    )
    resolution = hydration.resolve_policy(conn, "t1", "m1", asset_kind="audio")
    assert resolution.mode == "stream"
    assert resolution.source == "sync_policies"


def test_a_soft_deleted_pin_is_ignored(conn: sqlite3.Connection):
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")
    _seed_playlist_pin(
        conn, machine_id="m1", playlist_id="p1", stable_id="t1", mode="pinned"
    )
    conn.execute("UPDATE playlist_pins SET deleted_at = 't1'")
    resolution = hydration.resolve_policy(conn, "t1", "m1", asset_kind="audio")
    assert resolution.mode == "stream"


def test_an_unconfigured_machine_raises_rather_than_defaulting(
    conn: sqlite3.Connection,
):
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    with pytest.raises(hydration.HydrationError) as excinfo:
        hydration.resolve_policy(conn, "t1", "m1", asset_kind="audio")
    assert "sync_policies" in str(excinfo.value)


def test_an_unknown_asset_kind_raises(conn: sqlite3.Connection):
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    with pytest.raises(hydration.HydrationError):
        hydration.resolve_policy(conn, "t1", "m1", asset_kind="waveform_png")


def test_a_track_with_no_content_hash_is_unavailable_not_guessed(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    _seed_track(conn, "t1", content_hash=None)
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")
    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache", cfg=cfg
    )
    assert source.origin == "unavailable"
    assert source.reason is not None and "content_hash" in source.reason


def test_a_presign_without_a_config_raises_instead_of_returning_nothing(
    conn: sqlite3.Connection, tmp_path: Path
):
    """ADR 06 consequence 2: no creds means pinned/cached only, loudly."""
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")
    with pytest.raises(hydration.HydrationError) as excinfo:
        hydration.resolve_playback_source(
            conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "c", cfg=None
        )
    assert "R2_ACCESS_KEY_ID" in str(excinfo.value)


def test_remote_content_hash_wins_over_the_ingest_copy(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """The R2 key was minted from the remote row's digest, so it is truth."""
    remote_digest = _sha(b"the uploaded bytes")
    _seed_track(conn, "t1", content_hash=_sha(b"a stale ingest guess"))
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")
    conn.execute(
        "INSERT INTO track_locations(stable_id, kind, role, remote_url, "
        "available, content_hash, created_at, updated_at) VALUES "
        "(?, 'remote', 'alternate', ?, 1, ?, 't0', 't0')",
        ("t1", asset_store.asset_object_key(remote_digest), remote_digest),
    )
    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "c", cfg=cfg
    )
    assert source.content_hash == remote_digest
    assert remote_digest in (source.url or "")


# --- LRU eviction --------------------------------------------------------


def _cache_entry(cache_dir: Path, body: bytes, atime: float) -> Path:
    path = _write(hydration.cache_path(cache_dir, _sha(body)), body)
    os.utime(path, (atime, atime))
    return path


def test_evict_cache_drops_least_recently_used_until_under_budget(
    tmp_path: Path,
):
    cache_dir = tmp_path / "cache"
    mb = hydration.BYTES_PER_MB
    oldest = _cache_entry(cache_dir, b"o" * mb, atime=1_000)
    middle = _cache_entry(cache_dir, b"m" * mb, atime=2_000)
    newest = _cache_entry(cache_dir, b"n" * mb, atime=3_000)

    result = hydration.evict_cache(cache_dir, budget_mb=1)

    assert result.budget_bytes == mb
    assert result.bytes_remaining <= result.budget_bytes
    assert set(result.evicted) == {oldest, middle}
    assert result.bytes_freed == 2 * mb
    assert not oldest.exists()
    assert not middle.exists()
    assert newest.exists()


def test_evict_cache_tiebreak_is_deterministic_across_runs(tmp_path: Path):
    """Same atime on two entries must not evict in filesystem-listing order."""
    victims: list[tuple[Path, ...]] = []
    for run in range(2):
        cache_dir = tmp_path / f"run{run}"
        for body in (b"a", b"b", b"c", b"d"):
            entry = _write(
                hydration.cache_path(cache_dir, _sha(body * 1024)), body * 1024
            )
            os.utime(entry, (5_000, 5_000))
        result = hydration.evict_cache(cache_dir, budget_mb=0)
        victims.append(
            tuple(p.relative_to(cache_dir) for p in result.evicted)
        )
    assert victims[0] == victims[1]
    assert len(victims[0]) == 4


def test_evict_cache_is_a_noop_when_already_under_budget(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    kept = _cache_entry(cache_dir, b"small", atime=1_000)
    result = hydration.evict_cache(cache_dir, budget_mb=10)
    assert result.evicted == ()
    assert result.bytes_freed == 0
    assert kept.exists()


def test_evict_cache_treats_a_missing_dir_as_empty(tmp_path: Path):
    result = hydration.evict_cache(tmp_path / "never-created", budget_mb=5)
    assert result.evicted == ()
    assert result.bytes_remaining == 0


def test_evict_cache_rejects_a_negative_budget(tmp_path: Path):
    with pytest.raises(hydration.HydrationError):
        hydration.evict_cache(tmp_path, budget_mb=-1)


# --- push-then-delete ----------------------------------------------------


def test_push_then_delete_keeps_a_pinned_local_file(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    body = b"produced stems"
    produced = _write(tmp_path / "out" / "bundle.zip", body)
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")

    outcome = hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
    )
    assert outcome.local_action == "kept"
    assert outcome.uploaded is True
    assert outcome.local_path == produced
    assert produced.exists()
    assert asset_store.object_exists(cfg, fake_s3, _sha(body)) is True

    row = conn.execute(
        "SELECT remote_url, content_hash, available, origin_device_id "
        "FROM track_locations WHERE stable_id = 't1' AND kind = 'remote'"
    ).fetchone()
    assert row == (outcome.object_key, _sha(body), 1, "m1")


def test_push_then_delete_moves_a_cached_file_into_the_cache(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    body = b"produced stems"
    produced = _write(tmp_path / "out" / "bundle.zip", body)
    cache_dir = tmp_path / "cache"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="stem_bundle")
    _seed_local_location(conn, "t1", produced)

    outcome = hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
        cache_dir=cache_dir,
    )
    assert outcome.local_action == "cached"
    assert not produced.exists()
    assert outcome.local_path == hydration.cache_path(cache_dir, _sha(body))
    assert outcome.local_path is not None
    assert outcome.local_path.read_bytes() == body
    available = conn.execute(
        "SELECT available FROM track_locations WHERE stable_id='t1' "
        "AND kind='local'"
    ).fetchone()
    assert available == (0,)


def test_push_then_delete_needs_a_cache_dir_before_it_moves_anything(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    produced = _write(tmp_path / "out" / "bundle.zip", b"produced stems")
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="stem_bundle")
    with pytest.raises(hydration.HydrationError):
        hydration.apply_policy_after_produce(
            conn,
            fake_s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=produced,
            asset_kind="stem_bundle",
        )
    assert produced.exists()


@pytest.mark.parametrize("mode", ["stream", "excluded"])
def test_push_then_delete_removes_the_local_copy_under_stream_and_excluded(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
    mode: str,
):
    body = b"produced stems"
    produced = _write(tmp_path / "out" / "bundle.zip", body)
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", mode, asset_kind="stem_bundle")

    outcome = hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=produced,
        asset_kind="stem_bundle",
    )
    assert outcome.local_action == "deleted"
    assert outcome.local_path is None
    assert not produced.exists()
    # Deleted locally ONLY because the bytes are readable back from R2.
    assert asset_store.object_exists(cfg, fake_s3, _sha(body)) is True


def test_a_failed_upload_never_deletes_the_local_copy(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """The single irreversible step in the write path, guarded."""
    s3 = InMemoryAssetS3(fail_puts=True)
    produced = _write(tmp_path / "out" / "bundle.zip", b"produced stems")
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream", asset_kind="stem_bundle")

    with pytest.raises((asset_store.AssetStoreError, hydration.HydrationError)):
        hydration.apply_policy_after_produce(
            conn,
            s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=produced,
            asset_kind="stem_bundle",
        )
    assert produced.exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE kind='remote'"
    ).fetchone() == (0,)


def test_push_then_delete_is_idempotent_for_a_second_producer_run(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    body = b"produced stems"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")

    first = hydration.apply_policy_after_produce(
        conn, fake_s3, cfg,
        stable_id="t1", machine_id="m1",
        local_path=_write(tmp_path / "a" / "bundle.zip", body),
        asset_kind="stem_bundle",
    )
    second = hydration.apply_policy_after_produce(
        conn, fake_s3, cfg,
        stable_id="t1", machine_id="m1",
        local_path=_write(tmp_path / "b" / "bundle.zip", body),
        asset_kind="stem_bundle",
    )
    assert first.object_key == second.object_key
    assert first.uploaded is True
    assert second.uploaded is False
    assert conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE kind='remote'"
    ).fetchone() == (1,)


def test_push_then_delete_fails_loudly_on_a_missing_produced_file(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "pinned", asset_kind="stem_bundle")
    with pytest.raises(hydration.HydrationError):
        hydration.apply_policy_after_produce(
            conn,
            fake_s3,
            cfg,
            stable_id="t1",
            machine_id="m1",
            local_path=tmp_path / "never-written.zip",
            asset_kind="stem_bundle",
        )


def test_produced_asset_becomes_a_cache_hit_on_the_next_read(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
):
    """End-to-end: write path feeds the read path without a download."""
    body = b"produced stems"
    cache_dir = tmp_path / "cache"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached", asset_kind="audio")

    hydration.apply_policy_after_produce(
        conn,
        fake_s3,
        cfg,
        stable_id="t1",
        machine_id="m1",
        local_path=_write(tmp_path / "out" / "song.flac", body),
        asset_kind="audio",
        cache_dir=cache_dir,
    )
    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=cache_dir, cfg=cfg
    )
    assert source.origin == "cache"
    assert source.content_hash == _sha(body)
    assert source.path is not None and source.path.read_bytes() == body
