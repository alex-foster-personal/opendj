"""CloudSync status says ``enabled`` only while a scheduler is actually beating.

The latent false-green this closes (critic finding, cloudsync levels map,
Fri 11 Sep 2026): ``status.read_status`` returned ``enabled=True`` whenever
``MDT_CLOUDSYNC_SCHEDULER=1`` and a hub URL were set, while nothing on main
ran a sync loop. Configuration is an intent; only a fresh heartbeat is
evidence that something is syncing.

  - [if] status reads enabled with config set and no scheduler loop [then] broken, [else stop].
  - [if] a fresh heartbeat does not flip enabled to true [then] broken, [else stop].
  - [if] a stale heartbeat still reads as running [then] broken, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.sync_hub import config as sync_config
from apps.sync_hub import heartbeat as sync_heartbeat
from apps.sync_hub import status as sync_status

pytestmark = pytest.mark.requirement("CAT-04")

_HUB = "http://hub.example.test:8686"
_NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


def _env_configured() -> dict[str, str]:
    return {"MDT_CLOUDSYNC_SCHEDULER": "1", "MDT_CLOUDSYNC_HUB_URL": _HUB}


def test_env_configured_without_a_loop_is_not_enabled(tmp_path: Path) -> None:
    """[if] env config alone makes status.enabled true [then] broken, [else stop]."""
    current = sync_status.read_status(tmp_path, env=_env_configured(), now=_NOW)

    assert current.configured is True
    assert current.running is False
    assert current.enabled is False
    assert current.heartbeat_at is None
    assert current.reason is not None and "not running" in current.reason


def test_file_configured_without_a_loop_is_not_enabled(tmp_path: Path) -> None:
    """[if] file config alone makes status.enabled true [then] broken, [else stop]."""
    sync_config.write_config(
        tmp_path,
        sync_config.CloudSyncConfig(enabled=True, hub_url=_HUB, machine_name=None),
    )
    current = sync_status.read_status(tmp_path, env={}, now=_NOW)

    assert (current.configured, current.running, current.enabled) == (True, False, False)
    assert current.endpoint == _HUB
    assert current.enabled_source == "file"


def test_fresh_heartbeat_makes_a_configured_machine_enabled(tmp_path: Path) -> None:
    """[if] a fresh heartbeat on a configured machine is not enabled [then] broken, [else stop]."""
    sync_heartbeat.beat(tmp_path, hub_url=_HUB, now=_NOW)
    current = sync_status.read_status(
        tmp_path, env=_env_configured(), now=_NOW + timedelta(seconds=1)
    )

    assert (current.configured, current.running, current.enabled) == (True, True, True)
    assert current.reason is None
    assert current.heartbeat_at is not None


def test_stale_heartbeat_is_not_running(tmp_path: Path) -> None:
    """[if] a heartbeat past the stale bound reads as running [then] broken, [else stop]."""
    sync_heartbeat.beat(tmp_path, hub_url=_HUB, now=_NOW)
    later = _NOW + timedelta(seconds=sync_heartbeat.CFG.STALE_AFTER_S + 1)
    current = sync_status.read_status(tmp_path, env=_env_configured(), now=later)

    assert current.running is False
    assert current.enabled is False
    # The stale beat is still reported: an operator needs to see WHEN it died.
    assert current.heartbeat_at is not None


def test_heartbeat_without_configuration_is_not_enabled(tmp_path: Path) -> None:
    """[if] a leftover beat on an unconfigured machine reads enabled [then] broken, [else stop]."""
    sync_heartbeat.beat(tmp_path, hub_url=_HUB, now=_NOW)
    current = sync_status.read_status(tmp_path, env={}, now=_NOW)

    assert current.configured is False
    assert current.enabled is False
    assert current.reason == "CloudSync is not configured."
