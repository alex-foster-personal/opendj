"""First run with a default hub turns CloudSync on; an existing config always wins.

Contract: ``apps/sync_hub/first_run.py`` (issue #3870). A packaged build may
carry a default hub URL (``MDT_CLOUDSYNC_DEFAULT_HUB_URL`` at runtime, or the
``cloudsync.default_hub_url`` block of the payload manifest at build time).
On first run, with no ``cloudsync-config.json`` on disk, the engine writes
one with ``enabled: true`` and that URL, and the real scheduler picks it up
and beats. With no default the file is not written and status reads exactly
as it does today. A config file that already exists is never touched.

  - [if] a default hub is present, no config exists, and none is written [then] broken, [else stop].
  - [if] the seeded config does not make the real scheduler beat [then] broken, [else stop].
  - [if] no default hub is present and a config appears or status moves [then] broken, [else stop].
  - [if] an existing config file is rewritten on first run [then] broken, [else stop].
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.sync_hub import client, first_run
from apps.sync_hub import config as sync_config
from apps.sync_hub import scheduler as sync_scheduler
from apps.sync_hub import status as sync_status

pytestmark = pytest.mark.requirement("CLOUDSYNC-25")

_DEFAULT_HUB = "https://hub.example-tailnet.ts.net:8871"
_EXISTING_HUB = "http://127.0.0.1:8870"
_DEADLINE_S = 20.0
_FAST = sync_scheduler.SchedulerCfg(
    INTERVAL_S=0.2,
    INITIAL_DELAY_S=0.0,
    MAX_BACKOFF_S=0.8,
    BEAT_INTERVAL_S=0.05,
    STOP_TIMEOUT_S=15.0,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MDT_IS_HUB",
        sync_config.SCHEDULER_ENV,
        sync_config.ENDPOINT_ENV,
        first_run.DEFAULT_HUB_ENV,
        first_run.MANIFEST_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _spoke(tmp_path: Path) -> Path:
    data_dir = tmp_path / "spoke"
    data_dir.mkdir()
    state_db.open_rw(client.state_db_path(data_dir)).close()
    return data_dir


async def _until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {_DEADLINE_S}s waiting for {what}")
        await asyncio.sleep(0.02)


def _noop_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
    raise AssertionError("this test proves the loop beats; no round is expected to complete")


# ----- resolution ------------------------------------------------------------


def test_default_hub_comes_from_env_first(tmp_path: Path) -> None:
    """[if] the env var is set and does not win [then] broken, [else stop]."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(first_run.manifest_block("http://manifest.example.test")))
    env = {first_run.DEFAULT_HUB_ENV: _DEFAULT_HUB, first_run.MANIFEST_ENV: str(manifest)}
    assert first_run.resolve_default_hub_url(env) == _DEFAULT_HUB


def test_default_hub_comes_from_manifest_when_env_unset(tmp_path: Path) -> None:
    """[if] a manifest default is not read when the env is unset [then] broken, [else stop]."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"identity": {}, **first_run.manifest_block(_DEFAULT_HUB)}))
    resolved = first_run.resolve_default_hub_url({first_run.MANIFEST_ENV: str(manifest)})
    assert resolved == _DEFAULT_HUB


def test_manifest_without_cloudsync_block_means_no_default(tmp_path: Path) -> None:
    """[if] an older manifest with no cloudsync block yields a hub [then] broken, [else stop]."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"identity": {}}))
    assert first_run.resolve_default_hub_url({first_run.MANIFEST_ENV: str(manifest)}) is None
    assert first_run.manifest_block(None) == {"cloudsync": {"default_hub_url": None}}


@pytest.mark.parametrize("bad", ["hub.example.test", "ftp://x", " http://x ", ""])
def test_malformed_default_hub_is_refused_not_seeded(tmp_path: Path, bad: str) -> None:
    """[if] a malformed build default is silently accepted or dropped [then] broken, [else stop]."""
    if bad == "":
        assert first_run.resolve_default_hub_url({first_run.DEFAULT_HUB_ENV: bad}) is None
        return
    with pytest.raises(sync_config.CloudSyncConfigError):
        first_run.resolve_default_hub_url({first_run.DEFAULT_HUB_ENV: bad})
    with pytest.raises(sync_config.CloudSyncConfigError):
        first_run.manifest_block(bad)


# ----- seeding ---------------------------------------------------------------


def test_first_run_with_default_hub_writes_config_and_scheduler_beats(tmp_path: Path) -> None:
    """[if] first run with a default hub leaves sync off or silent [then] broken, [else stop]."""
    data_dir = _spoke(tmp_path)
    env = {first_run.DEFAULT_HUB_ENV: _DEFAULT_HUB}
    assert sync_config.read_config(data_dir) is None

    seed = first_run.seed_default_config(data_dir, env=env)

    assert seed.outcome == "seeded"
    assert seed.hub_url == _DEFAULT_HUB
    stored = sync_config.read_config(data_dir)
    assert stored == sync_config.CloudSyncConfig(
        enabled=True, hub_url=_DEFAULT_HUB, machine_name=None
    )
    before = sync_status.read_status(data_dir, env={})
    assert (before.configured, before.running) == (True, False)

    async def run() -> None:
        scheduler = sync_scheduler.CloudSyncScheduler(
            data_dir, cfg=_FAST, sync_fn=_noop_sync, env={}
        )
        await scheduler.start()
        try:
            await _until(
                lambda: sync_status.read_status(data_dir, env={}).running,
                "the scheduler to beat on the seeded config",
            )
        finally:
            await scheduler.stop()

    asyncio.run(run())
    after = sync_status.read_status(data_dir, env={})
    assert after.configured is True
    assert after.endpoint == _DEFAULT_HUB
    assert after.enabled_source == "file"


def test_first_run_without_default_hub_leaves_sync_off(tmp_path: Path) -> None:
    """[if] no default hub still writes a config or changes status [then] broken, [else stop]."""
    data_dir = _spoke(tmp_path)
    baseline = sync_status.read_status(data_dir, env={})

    seed = first_run.seed_default_config(data_dir, env={})

    assert seed.outcome == "no-default"
    assert seed.hub_url is None
    assert sync_config.read_config(data_dir) is None
    assert not sync_config.config_path(data_dir).exists()
    assert sync_status.read_status(data_dir, env={}) == baseline
    assert baseline.configured is False
    assert baseline.reason == "CloudSync is not configured."


def test_existing_config_is_never_overwritten(tmp_path: Path) -> None:
    """[if] first run rewrites an existing cloudsync-config.json [then] broken, [else stop]."""
    data_dir = _spoke(tmp_path)
    existing = sync_config.CloudSyncConfig(
        enabled=False, hub_url=_EXISTING_HUB, machine_name="kept"
    )
    path = sync_config.write_config(data_dir, existing)
    raw_before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns

    seed = first_run.seed_default_config(data_dir, env={first_run.DEFAULT_HUB_ENV: _DEFAULT_HUB})

    assert seed.outcome == "existing"
    assert seed.hub_url == _EXISTING_HUB
    assert path.read_bytes() == raw_before
    assert path.stat().st_mtime_ns == mtime_before
    assert sync_config.read_config(data_dir) == existing


def test_malformed_existing_config_is_left_alone_and_raised(tmp_path: Path) -> None:
    """[if] a malformed existing file is replaced by the default [then] broken, [else stop]."""
    data_dir = _spoke(tmp_path)
    path = sync_config.config_path(data_dir)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(sync_config.CloudSyncConfigError):
        first_run.seed_default_config(data_dir, env={first_run.DEFAULT_HUB_ENV: _DEFAULT_HUB})
    assert path.read_text(encoding="utf-8") == "{not json"


def test_hub_process_never_seeds(tmp_path: Path) -> None:
    """[if] a hub (MDT_IS_HUB=1) seeds itself a spoke config [then] broken, [else stop]."""
    data_dir = _spoke(tmp_path)
    env = {first_run.DEFAULT_HUB_ENV: _DEFAULT_HUB, "MDT_IS_HUB": "1"}

    seed = first_run.seed_default_config(data_dir, env=env)

    assert seed.outcome == "hub"
    assert sync_config.read_config(data_dir) is None
