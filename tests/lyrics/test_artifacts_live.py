"""One REAL R2 round-trip for the ``karaoke_words`` artifact.

Skipped unless ``R2_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` /
``R2_SECRET_ACCESS_KEY`` are in the environment, which in practice means::

    doppler run -p general -c dev_personal -- \\
        uv run pytest tests/lyrics/test_artifacts_live.py -m live_r2

Same skip contract as ``tests/cloudsync/test_asset_tier_live.py``, and the
same reason for existing: the in-memory ``InMemoryAssetS3`` double next door
proves the SEQUENCE (resolve policy, push, verify, fetch by hash) but cannot
prove that the production client, the real bucket and the real credentials
agree about it. This file closes that gap for the one asset kind whose only
location record is ``lyric_verdict.words_content_hash`` -- if a words artifact
cannot be fetched back by hash, the row is a dangling pointer and the track
page shows nothing.

Nothing is mocked. The client comes from
:func:`apps.lyrics.artifacts.asset_clients_for_mode`, i.e. exactly what the
CLI builds in cloud mode, so a broken credential path fails HERE rather than
in a venue. The payload carries a uuid so the digest is unique to this run,
and the object is deleted in a ``finally``: leaving litter in the canonical
asset bucket is how a bucket stops being trustworthy.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from apps.cloud import asset_store
from apps.lyrics import artifacts, karaoke_cache, store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from tests.cloudsync.conftest import LIVE_R2_ENV_VARS, live_r2_config

from .conftest import seed_stamped_policy, seed_track, use_cloud_mode, word

pytestmark = pytest.mark.live_r2

STABLE_ID: str = "live-words-probe"
COMPUTED_AT: str = "2026-09-09T00:00:00.000000+00:00"


def test_live_r2_words_artifact_round_trip(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Produce, push, drop the local file, and load it back by hash alone."""
    if live_r2_config() is None:
        pytest.skip(
            "live R2 creds absent; set "
            + ", ".join(LIVE_R2_ENV_VARS)
            + " (via `doppler run -p general -c dev_personal --`) to run this."
        )

    use_cloud_mode(monkeypatch)
    s3, cfg = artifacts.asset_clients_for_mode(writing=True)
    assert s3 is not None and cfg is not None, "cloud mode must build real clients"

    # Unique text so the digest is one no other run can already hold, which is
    # what makes the delete in the finally block safe to assert on.
    probe = f"probe-{uuid.uuid4().hex}"
    seed_track(conn, STABLE_ID)
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")

    artifact = artifacts.produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id=STABLE_ID,
        source="live-probe",
        words=[word(probe, start_s=1.0, end_s=1.2), word("two", line_final=True)],
        s3=s3,
        cfg=cfg,
    )
    try:
        assert asset_store.object_exists(cfg, s3, artifact.content_hash), (
            "produce_words_artifact reported a pinned push that R2 cannot HEAD"
        )
        store.upsert_verdict(
            conn,
            stable_id=STABLE_ID,
            verdict="vocal",
            coverage_pct=80.0,
            source="live-probe",
            language_iso3="eng",
            n_words=artifact.n_words,
            n_lines=artifact.n_lines,
            pct_witness_red=None,
            pipeline_version=PIPELINE_VERSION,
            words_content_hash=artifact.content_hash,
            computed_at=COMPUTED_AT,
            resurrect=False,
        )

        # The only location record is the row's hash, so removing the local
        # file is the whole test: what comes back must come from R2.
        artifact.path.unlink()
        loaded = artifacts.load_words(
            conn, data_dir=data_dir, stable_id=STABLE_ID, s3=s3, cfg=cfg
        )
        assert loaded is not None
        assert [entry.word for entry in loaded.words] == [probe, "two"]
        assert loaded.pipeline_version == PIPELINE_VERSION
        assert artifact.path == karaoke_cache.cache_path(data_dir, STABLE_ID)
        assert artifact.path.is_file(), "the fetch lands at the cache path"
    finally:
        removed = asset_store.delete_asset(cfg, s3, artifact.content_hash)
        assert removed, (
            f"live probe object for {artifact.content_hash} could not be "
            "deleted; remove it by hand before rerunning."
        )
    assert not asset_store.object_exists(cfg, s3, artifact.content_hash)
