"""The karaoke_words producer and reader over the content-addressed tier.

Policy mode is an import-time snapshot (``apps.cloud.policy.CFG``), so these
tests flip it by setting ``MUSIC_DJ_CLOUDSYNC_MODE`` AND re-running
``load_policy()`` through the explicit ``use_cloud_mode`` /
``use_local_mode`` helpers in ``conftest`` -- stated here rather than
implied, because a test that only set the env var would silently keep testing
whatever mode the process started in.

- if a produce writes a track_locations row then an AUDIO resolve of that
  track can come back holding the lyrics JSON's digest -- broken
- if local mode touches the network at all then a local-only install leaks
  licensed text -- broken
- if a pinned push is recorded without an object_exists check then a row can
  point at an object that is not durable -- broken
- if a missing sync_policies row is defaulted rather than raised, or
  cached/stream is silently accepted, then policy stops meaning anything --
  broken
- if load_words serves a local file whose sha256 disagrees with the row then
  a stale or tampered cache is indistinguishable from the real words -- broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.cloud import asset_store, hydration_core
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.lyrics import artifacts, karaoke_cache, store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION

from .conftest import (
    InMemoryAssetS3,
    seed_stamped_policy,
    seed_track,
    use_cloud_mode,
    use_local_mode,
    word,
)

WORDS = [word("one", start_s=1.0, end_s=1.2), word("two", start_s=1.5, line_final=True)]
AUDIO_DIGEST = "b" * 64


@pytest.mark.requirement("LYR-08")
def test_produce_strips_phantom_tail_and_keeps_source(
    conn: sqlite3.Connection,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] weak thank-you after silence and a word past the duration on produce [then] both
    omitted from the artifact [else stop]."""
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-tail")
    conn.execute(
        "UPDATE tracks SET duration_ms = ? WHERE stable_id = ?",
        (100_000, "sid-tail"),
    )
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("thank", start_s=95.0, end_s=95.3, witness="contradict", line_final=False),
        word("you", start_s=95.3, end_s=95.6, witness="lost", line_final=False),
        word("ghost", start_s=100.5, end_s=101.0, witness="agree", line_final=False),
    ]
    artifact = artifacts.produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id="sid-tail",
        source="lrclib get",
        words=words,
        s3=None,
        cfg=None,
    )
    parsed = karaoke_cache.parse_words(artifact.path.read_bytes(), "sid-tail")
    assert [w.word for w in parsed.words] == ["sing"]
    assert parsed.source == "lrclib get"


@pytest.mark.requirement("LYR-08")
def test_produce_keeps_real_final_words_inside_duration(
    conn: sqlite3.Connection,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] real lyrics end inside the duration, incl. a repeated chorus and a word in the
    final quarter-second [then] every word is in the artifact [else stop]."""
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-keep")
    conn.execute(
        "UPDATE tracks SET duration_ms = ? WHERE stable_id = ?",
        (100_000, "sid-keep"),
    )
    words = [word("verse", start_s=1.0, end_s=1.5, witness="agree", line_final=True)]
    t = 90.0
    for _ in range(4):
        words.append(word("thank", start_s=t, end_s=t + 0.3, witness="agree"))
        words.append(word("you", start_s=t + 0.3, end_s=t + 0.6, witness="agree"))
        t += 1.0
    words.append(word("now", start_s=99.9, end_s=100.0, witness="agree", line_final=True))
    artifact = artifacts.produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id="sid-keep",
        source="lrclib get",
        words=words,
        s3=None,
        cfg=None,
    )
    parsed = karaoke_cache.parse_words(artifact.path.read_bytes(), "sid-keep")
    assert [w.word for w in parsed.words] == [str(w["word"]) for w in words]


def _produce(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    stable_id: str = "sid-1",
    s3: InMemoryAssetS3 | None = None,
    cfg: CloudConfig | None = None,
) -> artifacts.WordsArtifact:
    return artifacts.produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id=stable_id,
        source="lrclib get",
        words=WORDS,
        s3=s3,
        cfg=cfg,
    )


def _record(conn: sqlite3.Connection, artifact: artifacts.WordsArtifact, stable_id: str) -> None:
    store.upsert_verdict(
        conn,
        stable_id=stable_id,
        verdict="vocal",
        coverage_pct=80.0,
        source="lrclib get",
        language_iso3="eng",
        n_words=artifact.n_words,
        n_lines=artifact.n_lines,
        pct_witness_red=None,
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=artifact.content_hash,
        computed_at="2026-09-01T00:00:00.000000+00:00",
        resurrect=False,
    )


#-----------------------------------------------------------------------------
# local mode
#-----------------------------------------------------------------------------
def test_local_mode_writes_the_file_and_touches_no_network(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-1")
    artifact = _produce(conn, data_dir, s3=fake_s3, cfg=None)
    assert artifact.path == karaoke_cache.cache_path(data_dir, "sid-1")
    assert artifact.path.is_file()
    assert (artifact.n_words, artifact.n_lines) == (2, 1)
    assert fake_s3.head_calls == [] and fake_s3.put_calls == []


def test_local_mode_needs_no_sync_policies_row(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-1")
    assert _produce(conn, data_dir, s3=None, cfg=None).content_hash


#-----------------------------------------------------------------------------
# cloud mode
#-----------------------------------------------------------------------------
def test_pinned_pushes_verifies_and_writes_no_track_location(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1", content_hash=AUDIO_DIGEST)
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    before = hydration_core._content_hash(conn, "sid-1")
    artifact = _produce(conn, data_dir, s3=fake_s3, cfg=cfg)
    key = asset_store.asset_object_key(artifact.content_hash)
    assert (cfg.audio_bucket, key) in fake_s3.store
    assert asset_store.object_exists(cfg, fake_s3, artifact.content_hash)
    assert artifact.path.is_file(), "pinned keeps the local file where it was written"
    assert conn.execute("SELECT COUNT(*) FROM track_locations").fetchone()[0] == 0
    assert hydration_core._content_hash(conn, "sid-1") == before == AUDIO_DIGEST


def test_excluded_pushes_nothing_and_keeps_the_local_file(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="excluded")
    artifact = _produce(conn, data_dir, s3=fake_s3, cfg=cfg)
    assert fake_s3.put_calls == []
    assert artifact.path.is_file()


@pytest.mark.parametrize("mode", ["cached", "stream"])
def test_cached_and_stream_are_refused_by_name(
    mode: str,
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode=mode)
    with pytest.raises(HydrationError, match=f"policy mode '{mode}'"):
        _produce(conn, data_dir, s3=fake_s3, cfg=cfg)


def test_a_missing_sync_policies_row_is_a_hard_error(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing on main seeds sync_policies; the runbook PUTs the cell once."""
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    with pytest.raises(HydrationError, match="no sync_policies row"):
        _produce(conn, data_dir, s3=fake_s3, cfg=cfg)


def test_cloud_mode_refuses_a_missing_client_or_config(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    with pytest.raises(HydrationError, match="no S3 client / CloudConfig"):
        _produce(conn, data_dir, s3=fake_s3, cfg=None)


def test_a_push_that_does_not_land_is_not_recorded(
    conn: sqlite3.Connection,
    data_dir: Path,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    with pytest.raises(asset_store.AssetStoreError, match="still absent"):
        _produce(conn, data_dir, s3=InMemoryAssetS3(fail_puts=True), cfg=cfg)


#-----------------------------------------------------------------------------
# the read side
#-----------------------------------------------------------------------------
def test_load_words_parses_the_verified_local_file(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-1")
    _record(conn, _produce(conn, data_dir, s3=None, cfg=None), "sid-1")
    loaded = artifacts.load_words(conn, data_dir=data_dir, stable_id="sid-1", s3=None, cfg=None)
    assert loaded is not None
    assert [entry.word for entry in loaded.words] == ["one", "two"]


def test_load_words_refuses_a_stale_local_file(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-1")
    artifact = _produce(conn, data_dir, s3=None, cfg=None)
    _record(conn, artifact, "sid-1")
    artifact.path.write_bytes(artifact.path.read_bytes().replace(b"one", b"ONE"))
    with pytest.raises(HydrationError, match="stale or tampered"):
        artifacts.load_words(conn, data_dir=data_dir, stable_id="sid-1", s3=None, cfg=None)


def test_load_words_returns_none_when_nothing_is_known(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-1")
    assert artifacts.load_words(
        conn, data_dir=data_dir, stable_id="sid-1", s3=None, cfg=None
    ) is None


def test_load_words_hydrates_by_hash_in_cloud_mode(
    conn: sqlite3.Connection,
    data_dir: Path,
    tmp_path: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row's words_content_hash is the ONLY location record."""
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    artifact = _produce(conn, data_dir, s3=fake_s3, cfg=cfg)
    _record(conn, artifact, "sid-1")
    artifact.path.unlink()
    loaded = artifacts.load_words(
        conn, data_dir=data_dir, stable_id="sid-1", s3=fake_s3, cfg=cfg
    )
    assert loaded is not None
    assert [entry.word for entry in loaded.words] == ["one", "two"]
    assert artifact.path.is_file(), "the fetch lands at the cache path, by stable_id"


def test_local_mode_never_hydrates_from_r2(
    conn: sqlite3.Connection,
    data_dir: Path,
    fake_s3: InMemoryAssetS3,
    cfg: CloudConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    seed_track(conn, "sid-1")
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    artifact = _produce(conn, data_dir, s3=fake_s3, cfg=cfg)
    _record(conn, artifact, "sid-1")
    artifact.path.unlink()
    use_local_mode(monkeypatch)
    assert artifacts.load_words(
        conn, data_dir=data_dir, stable_id="sid-1", s3=fake_s3, cfg=cfg
    ) is None


def test_load_words_refuses_a_file_the_row_does_not_vouch_for(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_local_mode(monkeypatch)
    seed_track(conn, "sid-1")
    _produce(conn, data_dir, s3=None, cfg=None)
    with pytest.raises(HydrationError, match="records no words_content_hash"):
        artifacts.load_words(conn, data_dir=data_dir, stable_id="sid-1", s3=None, cfg=None)
