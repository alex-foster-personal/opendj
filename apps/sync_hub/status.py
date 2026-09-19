"""CloudSync's small, truthful status surface and bounded result journal.

The status endpoint and CLI read this module. Three separate facts, never
collapsed into one:

* ``configured`` -- the effective config (``apps.sync_hub.config``: the
  ``cloudsync-config.json`` file, with ``MDT_CLOUDSYNC_SCHEDULER`` /
  ``MDT_CLOUDSYNC_HUB_URL`` as env overrides that win) says sync is on and
  names a hub. That is INTENT.
* ``running`` -- a scheduler heartbeat (``apps.sync_hub.heartbeat``) is
  fresh. That is EVIDENCE.
* ``enabled`` -- both. Configuration alone never reads as enabled: that was
  a latent false-green while no loop existed on main.

No configuration means off, not a plausible healthy response.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from apps.shared.state import sync_stamp
from apps.sync_hub import config as sync_config
from apps.sync_hub import digest_diff, spoke_credential
from apps.sync_hub import heartbeat as sync_heartbeat
from apps.sync_hub.wire_version import UpdateRequiredState, parse_update_required

SCHEDULER_ENV: str = sync_config.SCHEDULER_ENV
ENDPOINT_ENV: str = sync_config.ENDPOINT_ENV
SIGNED_IN_AS_ENV: str = "MDT_CLOUDSYNC_SIGNED_IN_AS"
STATUS_FILENAME: str = "cloudsync-status.json"
MAX_RECENT_RESULTS: int = 5

#: The three verdicts one sync can end on, and the third is not decoration.
#: ``inconclusive`` means the run COMPLETED but its post-sync digest compare
#: could not be made: one side held rows out of the comparison (a stored stamp
#: it cannot order), so the tables that differ differ for a reason nobody
#: measured. Recording that as ``ok`` is the defect ``.claude/rules/
#: verification.md`` names -- an unmeasured subject rendered as a clean
#: result -- and recording it as ``error`` would be the opposite lie, since
#: rows really did move. A caller that treats non-``ok`` as failure keeps
#: working; one that treats non-``error`` as success no longer does, which is
#: the point.
ResultStatus = Literal["ok", "error", "inconclusive", "deferred"]

#: Every value :data:`ResultStatus` admits, as data, so the wire validator and
#: the type cannot drift apart.
RESULT_STATUSES: tuple[ResultStatus, ...] = ("ok", "error", "inconclusive", "deferred")


class CloudSyncStatusError(RuntimeError):
    """The local CloudSync result journal is malformed or inaccessible."""


@dataclass(frozen=True)
class SyncResult:
    """One completed push/pull attempt, recorded locally and JSON-safe."""

    finished_at: str
    status: ResultStatus
    message: str
    pushed: int
    pulled: int

    @classmethod
    def from_wire(cls, payload: object, *, source: Path) -> SyncResult:
        if not isinstance(payload, dict):
            raise CloudSyncStatusError(f"{source} result is not an object")
        expected = {"finished_at", "status", "message", "pushed", "pulled"}
        if set(payload) != expected:
            raise CloudSyncStatusError(
                f"{source} result fields are {sorted(payload)}, expected {sorted(expected)}"
            )
        if payload["status"] not in RESULT_STATUSES:
            raise CloudSyncStatusError(f"{source} result status is invalid")
        text_keys = ("finished_at", "message")
        if not all(isinstance(payload[key], str) and payload[key] for key in text_keys):
            raise CloudSyncStatusError(f"{source} result requires non-empty text fields")
        count_keys = ("pushed", "pulled")
        if not all(isinstance(payload[key], int) and payload[key] >= 0 for key in count_keys):
            raise CloudSyncStatusError(f"{source} result counts must be non-negative integers")
        return cls(**payload)

    def to_wire(self) -> dict[str, Any]:
        return {
            "finished_at": self.finished_at,
            "status": self.status,
            "message": self.message,
            "pushed": self.pushed,
            "pulled": self.pulled,
        }


@dataclass(frozen=True)
class CloudSyncStatus:
    """The common wire object for HTTP, UI, and the operator CLI."""

    enabled: bool
    configured: bool
    running: bool
    heartbeat_at: str | None
    enabled_source: sync_config.ConfigSource
    endpoint_source: sync_config.ConfigSource
    reason: str | None
    signed_in_as: str | None
    last_push_at: str | None
    last_pull_at: str | None
    last_result: dict[str, str] | None
    rows_pending: int | None
    endpoint: str | None
    recent_results: tuple[SyncResult, ...]
    update_required: UpdateRequiredState | None
    digest_diff: tuple[dict[str, str], ...] | None = None
    credential_notice: dict[str, str] | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "enabled": self.enabled,
            "configured": self.configured,
            "running": self.running,
            "heartbeat_at": self.heartbeat_at,
            "enabled_source": self.enabled_source,
            "endpoint_source": self.endpoint_source,
            "reason": self.reason,
            "signed_in_as": self.signed_in_as,
            "last_push_at": self.last_push_at,
            "last_pull_at": self.last_pull_at,
            "last_result": self.last_result,
            "rows_pending": self.rows_pending,
            "endpoint": self.endpoint,
            "recent_results": [result.to_wire() for result in self.recent_results],
        }
        if self.update_required is not None:
            wire["update_required"] = asdict(self.update_required)
        else:
            wire["update_required"] = None
        wire["digest_diff"] = (
            None if self.digest_diff is None else [dict(row) for row in self.digest_diff]
        )
        wire["credential_notice"] = self.credential_notice
        return wire


def status_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATUS_FILENAME


#: The effective config a malformed override or file resolves to for STATUS
#: purposes only: off, with the error as the reason. The scheduler never uses
#: this; it refuses to run on the same error.
_UNREADABLE_CONFIG = sync_config.EffectiveConfig(
    enabled=False,
    hub_url=None,
    machine_name=None,
    enabled_source="default",
    hub_url_source="default",
)


def _effective_config(
    data_dir: Path, env: Mapping[str, str]
) -> tuple[sync_config.EffectiveConfig, str | None]:
    """The effective config plus the reason it is not configured, if it is not.

    A malformed file or override is SHOWN as the reason, not raised: the
    status surface is where an operator goes to find out what is wrong.
    """
    try:
        effective = sync_config.resolve_config(data_dir, env=env)
    except sync_config.CloudSyncConfigError as exc:
        return _UNREADABLE_CONFIG, f"CloudSync config is invalid: {exc}"
    if not effective.enabled:
        return effective, "CloudSync is not configured."
    if effective.hub_url is None:
        return effective, "CloudSync is enabled but no hub_url is set."
    return effective, None


def read_results(data_dir: Path) -> tuple[SyncResult, ...]:
    """Read the bounded newest-first history, never turning bad data into ok."""
    path = status_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise CloudSyncStatusError(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CloudSyncStatusError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"results"}:
        raise CloudSyncStatusError(f"{path} must be an object with only results")
    rows = payload["results"]
    if not isinstance(rows, list):
        raise CloudSyncStatusError(f"{path} results must be a list")
    parsed = [SyncResult.from_wire(row, source=path) for row in rows]
    ordered = sorted(parsed, key=lambda result: result.finished_at, reverse=True)
    return tuple(ordered[:MAX_RECENT_RESULTS])


def journal_deferred(data_dir: Path, reason: str) -> None:
    """Record one scheduler-deferred round without treating it as a failure."""
    write_result(
        data_dir,
        SyncResult(
            finished_at=sync_stamp.canonical_now(),
            status="deferred",
            message=f"deferred: {reason}",
            pushed=0,
            pulled=0,
        ),
    )


def write_result(data_dir: Path, result: SyncResult) -> tuple[SyncResult, ...]:
    """Prepend one result and atomically retain only the last five attempts."""
    results = (result, *read_results(data_dir))[:MAX_RECENT_RESULTS]
    path = status_path(data_dir)
    temp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"results": [row.to_wire() for row in results]})
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        raise CloudSyncStatusError(f"cannot write {path}: {exc}") from exc
    return results


def _read_heartbeat(data_dir: Path) -> sync_heartbeat.Heartbeat | None:
    try:
        return sync_heartbeat.read(data_dir)
    except sync_heartbeat.CloudSyncHeartbeatError as exc:
        raise CloudSyncStatusError(str(exc)) from exc


def _loop_evidence(
    data_dir: Path, now: datetime | None
) -> tuple[sync_heartbeat.Heartbeat | None, bool]:
    """The last heartbeat on file and whether it is fresh enough to prove a live loop."""
    beat = _read_heartbeat(data_dir)
    running = beat is not None and beat.is_fresh(datetime.now(UTC) if now is None else now)
    return beat, running


def _last_result_wire(latest: SyncResult | None) -> dict[str, str] | None:
    return None if latest is None else {"status": latest.status, "message": latest.message}


def read_status(
    data_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> CloudSyncStatus:
    """Return this machine's configured intent, loop evidence and recorded outcomes."""
    source = os.environ if env is None else env
    effective, reason = _effective_config(Path(data_dir), source)
    beat, running = _loop_evidence(Path(data_dir), now)
    if reason is None and not running:
        reason = "CloudSync is configured but its scheduler is not running (no fresh heartbeat)."
    results = read_results(Path(data_dir))
    latest = results[0] if results else None
    update_required = (
        None
        if latest is None or latest.status != "error"
        else parse_update_required(latest.message)
    )
    digest_samples = None
    if latest is not None and latest.status == "error":
        parsed = digest_diff.parse_digest_samples(latest.message)
        if parsed is not None:
            digest_samples = tuple(parsed)
    notice = spoke_credential.credential_notice(Path(data_dir))
    credential_notice_wire = (
        None
        if notice is None
        else {
            "verdict": notice.verdict,
            "action": notice.action,
            "hub_machine_id": notice.hub_machine_id,
        }
    )
    return CloudSyncStatus(
        enabled=effective.configured and running,
        configured=effective.configured,
        running=running,
        heartbeat_at=None if beat is None else beat.beat_at,
        enabled_source=effective.enabled_source,
        endpoint_source=effective.hub_url_source,
        reason=reason,
        signed_in_as=source.get(SIGNED_IN_AS_ENV, "").strip() or None,
        last_push_at=None if latest is None else latest.finished_at,
        last_pull_at=None if latest is None else latest.finished_at,
        last_result=_last_result_wire(latest),
        # A successful attempt's pushed/pulled counts describe movement in
        # opposite directions, not a backlog. Until the sync engine publishes
        # its watermark-derived count, claiming arithmetic here would lie.
        rows_pending=None,
        endpoint=effective.hub_url,
        recent_results=results,
        update_required=update_required,
        digest_diff=digest_samples,
        credential_notice=credential_notice_wire,
    )


__all__ = [
    "MAX_RECENT_RESULTS",
    "RESULT_STATUSES",
    "CloudSyncStatus",
    "CloudSyncStatusError",
    "SyncResult",
    "UpdateRequiredState",
    "journal_deferred",
    "read_status",
    "status_path",
    "write_result",
]
