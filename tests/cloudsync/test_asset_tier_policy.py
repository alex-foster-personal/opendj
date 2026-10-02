"""R2 asset tier: policy resolution matrix + LRU cache eviction (ADR 06).

Split out of ``test_asset_tier.py`` (round 4 quality-gate ratchet: that file
crossed 1070 lines, well past the 600-line "too long to hold in your head"
gate). Same contract, same fixtures (``tests/cloudsync/conftest.py``), same
helpers -- imported from ``test_asset_tier`` rather than duplicated so the two
files cannot drift on what a seeded track/policy/pin looks like.

Contract under test: ``specs/design_decision_06.md`` over the v6 policy
tables of ``specs/design_decision_05.md``. Acceptance criteria, one
assertion block each:

- if the policy matrix resolves any of pinned/cached/stream/excluded, or a
  playlist-pin override, differently from ADR 06 point 4, a gig crate can
  silently end up streaming over venue wifi -- broken.
- if eviction leaves the cache over budget, evicts the most-recently-used
  first, or picks a different victim on a rerun, the LRU is not an LRU --
  broken.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from apps.cloud import asset_store, hydration
from apps.cloud.config import CloudConfig

from .test_asset_tier import (
    _seed_local_location,
    _seed_machine,
    _seed_playlist_pin,
    _seed_policy,
    _seed_track,
    _sha,
    _write,
)

pytestmark = pytest.mark.requirement("CAT-04")


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


@pytest.mark.parametrize("mode", ["cached", "excluded"])
def test_malformed_content_hash_does_not_block_local_or_excluded_resolution(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path, mode: str
):
    """Only a remote read needs a valid digest-derived object key."""
    audio = _write(tmp_path / "music" / "song.flac", b"local body")
    _seed_track(conn, "t1", content_hash="not-a-sha256")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", mode)
    _seed_local_location(conn, "t1", audio)

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache", cfg=cfg
    )

    assert source.origin == ("unavailable" if mode == "excluded" else "local")
    assert source.content_hash == "not-a-sha256"


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


def test_unconfigured_machine_with_local_file_plays_without_policy_row(
    conn: sqlite3.Connection, tmp_path: Path
):
    """Local-only installs without a sync_policies row still play on-disk bytes."""
    body = b"local body"
    audio = _write(tmp_path / "music" / "song.flac", body)
    _seed_track(conn, "t1", content_hash=_sha(body))
    _seed_machine(conn, "m1")
    conn.execute(
        "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
        (str(audio), "t1"),
    )

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache"
    )
    assert source.origin == "local"
    assert source.path == audio


@pytest.mark.requirement("CLOUDSYNC-33")
def test_unconfigured_machine_without_a_local_file_is_local_only(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """[if] a machine has no sync_policies row and no copy of a track [then]
    playback resolution answers unavailable "not on this computer" and never
    presigns, [else stop]."""
    _seed_track(conn, "t1", content_hash=_sha(b"body"))
    _seed_machine(conn, "m1")
    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "cache", cfg=cfg
    )
    assert source.origin == "unavailable"
    assert source.policy_source == "unconfigured"
    assert source.reason == hydration.NOT_ON_THIS_MACHINE
    # resolve_policy itself still refuses to assume a mode.
    with pytest.raises(hydration.PolicyUnconfigured):
        hydration.resolve_policy(conn, "t1", "m1", asset_kind="audio")


@pytest.mark.requirement("CLOUDSYNC-33")
def test_unconfigured_machine_plays_bytes_a_past_policy_hydrated(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """[if] a machine with no sync_policies row still holds a hydrated cache
    entry for a track [then] it plays that entry and never presigns, [else
    stop]."""
    digest = _sha(b"body")
    _seed_track(conn, "t1", content_hash=digest)
    _seed_machine(conn, "m1")
    cache_dir = tmp_path / "cache"
    cached = hydration.cache_path(cache_dir, digest)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"body")
    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=cache_dir, cfg=cfg
    )
    assert source.origin == "cache"
    assert source.path == cached
    assert source.url is None
    # Control: without the entry the same machine says the file is not here.
    cached.unlink()
    gone = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=cache_dir, cfg=cfg
    )
    assert gone.origin == "unavailable"
    assert gone.reason == hydration.NOT_ON_THIS_MACHINE


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


def test_a_prefixed_stored_hash_still_resolves_to_a_content_addressed_key(
    conn: sqlite3.Connection, cfg: CloudConfig, tmp_path: Path
):
    """``tracks.content_hash`` is stored ``sha256:<hex>`` by the state layer;
    the read path must normalize it before it can name a cache entry or mint
    a presigned URL, or the backfilled hashes never address an object."""
    body = b"audio that ingest hashed with apps.shared.hashing"
    digest = _sha(body)
    _seed_track(conn, "t1", content_hash=f"sha256:{digest}")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")

    source = hydration.resolve_playback_source(
        conn, "t1", "m1", asset_kind="audio", cache_dir=tmp_path / "c", cfg=cfg
    )

    assert source.origin == "presigned"
    assert source.content_hash == digest  # bare hex, not sha256:<hex>
    assert digest in (source.url or "")


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


@pytest.mark.requirement("CLOUDSYNC-10")
def test_local_row_marked_unavailable_returns_unavailable_reason(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    tmp_path: Path,
):
    missing = tmp_path / "gone.flac"
    digest = _sha(b"body")
    _seed_track(conn, "t1", content_hash=digest)
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "stream")
    _seed_local_location(
        conn,
        "t1",
        missing,
        available=0,
        content_hash=digest,
        machine_id="m1",
    )
    source = hydration.resolve_playback_source(
        conn,
        "t1",
        "m1",
        asset_kind="audio",
        cache_dir=tmp_path / "cache",
        cfg=cfg,
    )
    assert source.origin == "unavailable"
    assert source.reason is not None
    assert "available=0" in source.reason
    assert str(missing) in source.reason


@pytest.mark.requirement("CLOUDSYNC-10")
def test_eviction_uses_cfg_audio_budget(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    fake_s3: InMemoryAssetS3,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.cloud import policy as cloud_policy

    monkeypatch.setattr(cloud_policy, "cache_budget_mb_for", lambda _kind: 1)
    cache_dir = tmp_path / "cache"
    for idx in range(3):
        _cache_entry(cache_dir, bytes([idx]) * 400_000, atime=1_000 + idx)
    body = b"fresh produce"
    _seed_track(conn, "t1")
    _seed_machine(conn, "m1")
    _seed_policy(conn, "m1", "cached")
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
    remaining = sum(
        path.stat().st_size for path in cache_dir.rglob("*") if path.is_file()
    )
    assert remaining <= 1 * hydration.BYTES_PER_MB

