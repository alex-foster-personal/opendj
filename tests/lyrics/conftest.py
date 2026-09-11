"""Shared fixtures for the PR-2 lyric storage suite.

Every DB here is a FILE under ``tmp_path`` opened through
``apps.shared.state.db.open_rw``, never in-memory: ``sync_stamp`` refuses a
connection with no database file (it derives the data dir, and therefore the
machine identity, from the DB path), so an in-memory DB cannot stamp a single
write. The canonical layout ``<data-dir>/state/state.db`` is what makes
``data_dir_for_connection`` return the same ``data_dir`` the karaoke cache is
written under.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.cloud import policy
from apps.cloud.config import CloudConfig
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp

# Re-exported so the lyric tests use the ONE sanctioned in-memory S3 double
# rather than minting a second one that can drift from it.
from tests.cloudsync.conftest import InMemoryAssetS3


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir: Path) -> Iterator[sqlite3.Connection]:
    """A migrated state DB at ``<data_dir>/state/state.db``."""
    connection = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def fake_s3() -> InMemoryAssetS3:
    return InMemoryAssetS3()


@pytest.fixture
def cfg() -> CloudConfig:
    """A fully populated config; credential absence is tested explicitly."""
    return CloudConfig(
        r2_account_id="acct1234",
        r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="test-host",
        bind_host="127.0.0.1",
    )


def seed_track(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    deleted_at: str | None = None,
    content_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at, deleted_at) "
        "VALUES (?, 'inferred', ?, ?, 't0', 't0', ?)",
        (stable_id, f"track {stable_id}", content_hash, deleted_at),
    )


def seed_stamped_policy(
    conn: sqlite3.Connection, *, asset_kind: str, mode: str
) -> str:
    """One ``sync_policies`` cell for THIS machine, stamped like a real write.

    ``tests/cloudsync/test_asset_tier._seed_policy`` writes ``updated_at='t0'``
    directly, which is fine for policy resolution but leaves no changelog
    entry; the lyric tests assert on changelog rows, so this seeds the way the
    PUT route does.
    """
    machine_id = sync_stamp.ensure_local_machine(conn)
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(
            conn, "sync_policies", (machine_id, asset_kind), machine_id
        )
        conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode, "
            "cache_budget_mb, updated_at, origin_device_id) "
            "VALUES (?, ?, ?, 512, ?, ?)",
            (machine_id, asset_kind, mode, stamp.updated_at, stamp.origin_device_id),
        )
    return machine_id


def word(
    text: str,
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    score: float | None = None,
    witness: str | None = None,
    asr_delta_s: float | None = None,
    line_final: bool = False,
) -> dict[str, object]:
    """One producer-input word dict, in the manifest's own shape."""
    return {
        "word": text,
        "start_s": start_s,
        "end_s": end_s,
        "score": score,
        "witness": witness,
        "asr_delta_s": asr_delta_s,
        "line_final": line_final,
    }


def use_cloud_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-resolve ``apps.cloud.policy.CFG`` in cloud mode, explicitly.

    ``CFG`` is an import-time snapshot, so setting the env var alone changes
    nothing for an already-imported module. The choice made here is to set the
    env var AND re-run ``load_policy()``, which yields a fully consistent
    policy object (artifact layouts and defaults included) rather than a
    hand-built one with only ``mode`` flipped.
    """
    monkeypatch.setenv(policy.MODE_ENV, "cloud")
    monkeypatch.setattr(policy, "CFG", policy.load_policy())


def use_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror of :func:`use_cloud_mode` for ``local``."""
    monkeypatch.setenv(policy.MODE_ENV, "local")
    monkeypatch.setattr(policy, "CFG", policy.load_policy())
