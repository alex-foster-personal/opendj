"""An unreachable hub on a live scheduler reads "Waiting for hub", not an error.

Issue #3870: a first run that was turned on by the build's default hub may
start before that hub answers (or before the tailnet route exists). The
scheduler keeps retrying with backoff, so the status must say so. The reason
carries :data:`apps.sync_hub.status.WAITING_FOR_HUB_PREFIX` only while the
config is on AND the heartbeat proves the loop is alive; a non-transport error
(digest mismatch, protocol refusal) is never softened into a wait.

  - [if] a live loop cannot reach the hub and is not Waiting for hub [then] broken, [else stop].
  - [if] a dead loop or an unconfigured machine reports Waiting for hub [then] broken, [else stop].
  - [if] a non-transport error is reported as Waiting for hub [then] broken, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.sync_hub import config as sync_config
from apps.sync_hub import heartbeat as sync_heartbeat
from apps.sync_hub import status as sync_status

pytestmark = pytest.mark.requirement("CLOUDSYNC-25")

_HUB = "https://hub.example-tailnet.ts.net:8871"
_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
#: The journal line maintenance.sync writes for a refused connection.
_UNREACHABLE = (
    "could not reach the hub machine: SYNC_HUB_UNREACHABLE hub unreachable at "
    f"{_HUB}: <urlopen error [Errno 111] Connection refused>"
)
_DIGEST = "SyncDigestMismatch: tracks differ after settling (2 rows)"


def _configured(data_dir: Path) -> None:
    sync_config.write_config(
        data_dir, sync_config.CloudSyncConfig(enabled=True, hub_url=_HUB, machine_name=None)
    )


def _journal_error(data_dir: Path, message: str) -> None:
    sync_status.write_result(
        data_dir,
        sync_status.SyncResult(
            finished_at=_NOW.isoformat(), status="error", message=message, pushed=0, pulled=0
        ),
    )


def test_live_loop_with_unreachable_hub_is_waiting(tmp_path: Path) -> None:
    """[if] a beating loop with a dead hub is not Waiting for hub [then] broken, [else stop]."""
    _configured(tmp_path)
    sync_heartbeat.beat(tmp_path, hub_url=_HUB, now=_NOW)
    _journal_error(tmp_path, _UNREACHABLE)

    current = sync_status.read_status(tmp_path, env={}, now=_NOW)

    assert (current.configured, current.running, current.enabled) == (True, True, True)
    assert current.reason is not None
    assert current.reason.startswith(sync_status.WAITING_FOR_HUB_PREFIX)
    assert "could not reach the hub machine" in current.reason
    assert "retries in the background" in current.reason
    assert current.last_result == {"status": "error", "message": _UNREACHABLE}


def test_dead_loop_is_not_waiting(tmp_path: Path) -> None:
    """[if] no fresh heartbeat still reads Waiting for hub [then] broken, [else stop]."""
    _configured(tmp_path)
    _journal_error(tmp_path, _UNREACHABLE)

    current = sync_status.read_status(tmp_path, env={}, now=_NOW)

    assert current.running is False
    assert current.reason is not None
    assert not current.reason.startswith(sync_status.WAITING_FOR_HUB_PREFIX)
    assert "not running" in current.reason


def test_unconfigured_machine_is_not_waiting(tmp_path: Path) -> None:
    """[if] an unconfigured machine with an old error reads Waiting [then] broken, [else stop]."""
    _journal_error(tmp_path, _UNREACHABLE)

    current = sync_status.read_status(tmp_path, env={}, now=_NOW)

    assert current.configured is False
    assert current.reason == "CloudSync is not configured."


def test_non_transport_error_is_not_waiting(tmp_path: Path) -> None:
    """[if] a digest mismatch is softened into Waiting for hub [then] broken, [else stop]."""
    _configured(tmp_path)
    sync_heartbeat.beat(tmp_path, hub_url=_HUB, now=_NOW)
    _journal_error(tmp_path, _DIGEST)

    current = sync_status.read_status(tmp_path, env={}, now=_NOW)

    assert current.running is True
    assert current.reason is None
    assert sync_status.waiting_for_hub_reason(None) is None
