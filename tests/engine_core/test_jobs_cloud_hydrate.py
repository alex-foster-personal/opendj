"""``cloud.hydrate`` job kind: enqueue, argv, and runner terminal states.

[if] a cloud.hydrate job runs through the engine job runner [then] enqueue argv and terminal states stay contract-stable, [else stop].
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.cloud import hydration
from apps.cloud import job as cloud_job
from apps.cloud.config import CloudConfig
from apps.engine_core.jobs.runner import (
    JobRunner,
    register_reconcile,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("CLOUDSYNC-10")

_OK_WORKER = (
    "import json; print(json.dumps({'progress': 1.0, 'message': 'ok'}), flush=True)"
)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _seed_state(
    state_path: Path,
    *,
    stable_id: str,
    machine_id: str,
    content_hash: str,
    mode: str = "stream",
) -> None:
    import sqlite3

    conn = sqlite3.connect(state_path)
    conn.execute("PRAGMA foreign_keys = ON")
    state_schema.apply_migrations(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES (?, 'inferred', ?, ?, 't0', 't0')",
        (stable_id, stable_id, content_hash),
    )
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, data_root, "
        "first_seen, last_seen) VALUES (?, ?, 'linux', 0, ?, 't0', 't0')",
        (machine_id, machine_id, str(state_path.parent.parent)),
    )
    conn.execute(
        "INSERT INTO sync_policies(machine_id, asset_kind, mode, cache_budget_mb, "
        "updated_at) VALUES (?, 'audio', ?, 100, 't0')",
        (machine_id, mode),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> CloudConfig:
    for name, value in {
        "R2_ACCOUNT_ID": "acct",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
    }.items():
        monkeypatch.setenv(name, value)
    return CloudConfig.from_env()


@pytest.fixture
def kind() -> Iterator[None]:
    register_worker(cloud_job.JOB_KIND, cloud_job.build_argv)
    register_worker("cloud.hydrate-test-ok", lambda _p: [sys.executable, "-c", _OK_WORKER])
    register_reconcile(cloud_job.JOB_KIND, cloud_job.reconcile_from_disk)
    yield
    unregister_worker(cloud_job.JOB_KIND)
    unregister_worker("cloud.hydrate-test-ok")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[JobStore]:
    opened = JobStore(tmp_path / "jobs.db", boot_id="boot-hydrate", owner_pid=os.getpid())
    opened.recover()
    yield opened
    opened.close()


def test_enqueue_hydrate_asset_persists_row(store: JobStore, tmp_path: Path) -> None:
    job = hydration.enqueue_hydrate_asset(
        store,
        stable_id="a" * 40,
        asset_kind="audio",
        machine_id="m-hydrate",
        data_dir=tmp_path / "data",
    )
    assert job["kind"] == cloud_job.JOB_KIND
    assert job["status"] == "queued"
    assert job["payload"]["stable_id"] == "a" * 40


def test_enqueue_without_store_raises() -> None:
    with pytest.raises(hydration.HydrationError, match="jobs store"):
        hydration.enqueue_hydrate_asset(
            None,
            stable_id="a" * 40,
            asset_kind="audio",
            machine_id="m1",
            data_dir=Path("/tmp/data"),
        )


def test_enqueue_without_jobs_db_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "jobs.db"
    opened = JobStore(missing, boot_id="boot-missing", owner_pid=os.getpid())
    opened.close()
    missing.unlink()
    assert not missing.is_file()

    with pytest.raises(hydration.HydrationError, match="jobs database missing"):
        hydration.enqueue_hydrate_asset(
            SimpleNamespace(db_path=missing),
            stable_id="a" * 40,
            asset_kind="audio",
            machine_id="m1",
            data_dir=tmp_path / "data",
        )


def test_cloud_hydrate_kind_reaches_terminal_status(
    store: JobStore, tmp_path: Path
) -> None:
    register_worker(cloud_job.JOB_KIND, lambda _p: [sys.executable, "-c", _OK_WORKER])
    register_reconcile(cloud_job.JOB_KIND, cloud_job.reconcile_from_disk)
    payload = {
        "stable_id": "a" * 40,
        "asset_kind": "audio",
        "machine_id": "m1",
        "data_dir": str(tmp_path / "data"),
    }
    job = store.enqueue(cloud_job.JOB_KIND, payload=payload)
    assert job["kind"] == cloud_job.JOB_KIND
    assert job["status"] == "queued"

    async def _drive() -> dict:
        runner = JobRunner(store, poll_s=0.05)
        await runner.start()
        try:
            for _ in range(100):
                row = store.get(job["id"])
                if row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                    return row
                await asyncio.sleep(0.05)
            raise AssertionError("job never reached terminal state")
        finally:
            await runner.stop()
            unregister_worker(cloud_job.JOB_KIND)

    terminal = asyncio.run(_drive())
    assert terminal["status"] == "succeeded"


def test_runner_marks_terminal_status(store: JobStore, kind: None) -> None:
    job = store.enqueue("cloud.hydrate-test-ok", payload={})

    async def _drive() -> dict:
        runner = JobRunner(store, poll_s=0.05)
        await runner.start()
        try:
            for _ in range(100):
                row = store.get(job["id"])
                if row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                    return row
                await asyncio.sleep(0.05)
            raise AssertionError("job never reached terminal state")
        finally:
            await runner.stop()

    terminal = asyncio.run(_drive())
    assert terminal["status"] == "succeeded"


def test_reconcile_from_disk_succeeds_when_cache_present(tmp_path: Path) -> None:
    stable_id = "d" * 40
    machine_id = "m-reconcile"
    body = b"reconcile-body"
    digest = _sha(body)
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_state(state_path, stable_id=stable_id, machine_id=machine_id, content_hash=digest)
    cache_dir = data_dir / "state" / "audio-cache"
    dest = cache_dir / digest[:2] / digest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    payload = {
        "stable_id": stable_id,
        "asset_kind": "audio",
        "machine_id": machine_id,
        "data_dir": str(data_dir),
    }
    assert cloud_job.reconcile_from_disk({"payload": payload}) == "succeeded"
