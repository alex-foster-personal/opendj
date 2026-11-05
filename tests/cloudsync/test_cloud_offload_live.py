"""One REAL offload and restore against R2 (OFFLOAD, spec e8da14b8 section 6).

Skipped unless ``R2_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` /
``R2_SECRET_ACCESS_KEY`` are in the environment, gated exactly like
``test_asset_tier_live.py``::

    doppler run -p general -c dev_personal -- \\
        uv run pytest tests/cloudsync/test_cloud_offload_live.py -m live_r2

What it proves that no fake can: R2 answers a single-part PUT with an ETag
equal to the body's MD5 (the premise of the verify rule and of the in-memory
fake's ETag), the production boto3 adapter streams a restore, and the whole
offload -> restore round trip leaves byte-identical audio. Audio only, so the
fleet's shared stem index pointer is never touched; the one object written is
deleted in a ``finally``.

- [if] real R2 answers a single-part PUT [then] its ETag is the body MD5 and the round trip is byte-identical, [else stop].
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

import pytest

from apps.cloud import asset_store, offload
from apps.cloud import offload_ledger as ledger
from apps.cloud.lock import FakeS3Client
from apps.cloud.offload_verify import boto3_offload_client
from apps.shared.state import sync_stamp

from .conftest import LIVE_R2_ENV_VARS, live_r2_config

pytestmark = [pytest.mark.live_r2, pytest.mark.requirement("OFFLOAD-8598")]


def test_live_offload_and_restore_round_trip(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cfg = live_r2_config()
    if cfg is None:
        pytest.skip(
            "live R2 creds absent; set "
            + ", ".join(LIVE_R2_ENV_VARS)
            + " (via `doppler run -p general -c dev_personal --`) to run this."
        )
    assert cfg is not None
    body = f"music-dj-tools offload live probe {uuid.uuid4().hex}".encode() * 512
    digest = hashlib.sha256(body).hexdigest()
    audio = tmp_path / "music" / "live.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(body)
    stable_id = "live-offload-" + uuid.uuid4().hex[:8]
    sync_stamp.ensure_local_machine(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, 'inferred', 'live', ?, 't0', 't0')",
        (stable_id, str(audio)),
    )
    conn.commit()
    assets = boto3_offload_client(cfg)
    ctx = offload.OffloadContext(
        data_dir=tmp_path,
        state_db=tmp_path / "state.db",
        stems_dir=tmp_path / "no-stems",
        cfg=cfg,
        assets=assets,
        index_s3=FakeS3Client(),  # never used: no stems, so no index publish
        protected_ids=lambda: frozenset(),
        gate_reason=lambda: None,
    )
    try:
        outcome = offload.offload_track(ctx, stable_id)
        assert outcome.state == "offloaded" and not audio.exists()
        head = assets.head_object(cfg.audio_bucket, asset_store.asset_object_key(digest))
        assert head is not None and head.size == len(body)
        assert head.etag.strip('"') == hashlib.md5(body, usedforsecurity=False).hexdigest()
        with ledger.open_ledger(tmp_path) as led:
            assert {f.verified_by for f in ledger.files_for(led, stable_id)} == {"etag-md5"}

        restored = offload.restore_track(ctx, stable_id, "temporary")
        assert restored.state == "temporary"
        assert audio.read_bytes() == body
    finally:
        asset_store.delete_asset(cfg, assets, digest)
    assert assets.head_object(cfg.audio_bucket, asset_store.asset_object_key(digest)) is None
