"""Background CloudSync scheduler: this spoke syncs with its hub on its own.

Design recovered from ``apps/cloud/scheduler.py`` at 9a1438b8 (PR #1470,
merged to a side branch and never to main), fitted to main:

* An asyncio task owned by the engine lifespan (:func:`scheduler_lifespan`),
  not a bare daemon thread. The sync itself runs OFF the event loop through
  ``asyncio.to_thread``: ``run_sync`` blocks on HTTP and takes SQLite write
  transactions on the same ``state.db`` that library listing and deck load
  read, so it must never run on the loop that serves them.
* It re-reads the effective config (``apps.sync_hub.config``) on every wake,
  so ``PUT /api/v1/cloudsync/config`` or ``python -m apps.sync_hub config
  set`` from another process takes effect without a restart. While
  unconfigured it only sleeps: no sync, no heartbeat.
* Single-flight. The loop never starts a round while one is in flight, and
  :meth:`CloudSyncScheduler.run_round` itself refuses to overlap (a
  non-blocking lock), so a second caller gets ``"busy"`` rather than a
  second concurrent sync.
* Exponential backoff on failure: the next round waits
  ``min(INTERVAL_S * 2**failures, MAX_BACKOFF_S)``.
* Journals every round through ``maintenance.sync``, which writes the
  ``cloudsync-status.json`` result (``ok`` / ``inconclusive`` / ``error``).
* Beats ``cloudsync-heartbeat.json`` while configured and alive, including
  during a long round, and deletes it on stop. ``status.enabled`` reads that
  beat, never the config alone.
* ``SyncDigestMismatch`` HALTS the loop (ADR 04 c6: settled and still
  diverged, no repair attempted), exactly as the 9a1438b8 design did.
* Never started on the hub (``MDT_IS_HUB=1``): the hub is the peer.

Not carried over from 9a1438b8: cache eviction, which belongs to the
``apps.cloud`` policy work (PR #1462).

CLOUDSYNC-14 (issue #2656 part 1): refuse while ``app_posture=gig`` or any
deck is playing unless Force sync (part 2). CLOUDSYNC-09 part 1 (issue
#2658): refuse under elevated machine pressure or session xruns while a deck
is playing; coalesce owed rounds via ``scheduler_owed`` and PERFMODE-04 shed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.shared.state import machine_identity
from apps.shared.sync_runtime_gates import (
    DEFER_REASON_GIG,
    DEFER_REASON_PRESSURE_SHED,
    SyncDeferredError,
    refuse_sync_round,
)
from apps.sync_hub import client as sync_client
from apps.sync_hub import config as sync_config
from apps.sync_hub import heartbeat as sync_heartbeat
from apps.sync_hub import maintenance
from apps.sync_hub import status as sync_status
from apps.sync_hub.scheduler_owed import (
    clear_scheduler_owed,
    mark_scheduler_owed,
    scheduler_owed,
)
from apps.sync_hub.single_flight import sync_lock_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerCfg:
    #: Seconds between completed rounds when the last one succeeded.
    INTERVAL_S: float = 300.0
    #: Seconds after start before the first round: keeps the first sync's
    #: SQLite writes out of the boot request burst (perf register, "Boot
    #: request window").
    INITIAL_DELAY_S: float = 30.0
    #: Ceiling on the failure backoff, in seconds.
    MAX_BACKOFF_S: float = 3600.0
    #: Seconds between wakes; each wake re-reads config and beats.
    BEAT_INTERVAL_S: float = sync_heartbeat.CFG.BEAT_INTERVAL_S
    #: Seconds stop() waits for an in-flight round before giving up on it.
    STOP_TIMEOUT_S: float = 10.0


CFG = SchedulerCfg()

RoundOutcome = Literal["ok", "inconclusive", "error", "halted", "busy", "deferred"]

#: ``(data_dir, hub_url, machine_name) -> SyncResult``; the production value
#: is ``maintenance.sync``, which journals the result.
SyncFn = Callable[[Path, str, "str | None"], sync_client.SyncResult]

UiMirrorProvider = Callable[[], Mapping[str, Any] | None]
PressureReader = Callable[[], Mapping[str, Any]]


def _default_pressure_reader() -> Mapping[str, Any]:
    from apps.shared.machine_pressure import read_machine_pressure

    return read_machine_pressure()


def next_delay_s(interval_s: float, consecutive_failures: int, max_backoff_s: float) -> float:
    """Seconds until the next round: the interval, doubled per consecutive failure."""
    return min(interval_s * (2**consecutive_failures), max_backoff_s)


def _production_sync(data_dir: Path, hub_url: str, name: str | None) -> sync_client.SyncResult:
    return maintenance.sync(data_dir, hub_url, name=name)


class CloudSyncScheduler:
    """One per data dir, per process. Start and stop it from the event loop."""

    def __init__(
        self,
        data_dir: Path,
        *,
        cfg: SchedulerCfg = CFG,
        sync_fn: SyncFn = _production_sync,
        env: Mapping[str, str] | None = None,
        ui_mirror_provider: UiMirrorProvider | None = None,
        pressure_reader: PressureReader | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._cfg = cfg
        self._sync_fn = sync_fn
        self._env = env
        self._ui_mirror_provider = ui_mirror_provider or (lambda: None)
        self._pressure_reader = pressure_reader or _default_pressure_reader
        # Shared with POST /cloudsync/sync (Sync now): one sync per data dir.
        self._round_lock = sync_lock_for(self._data_dir)
        self._next_due = 0.0
        self._last_config_error: str | None = None
        self._stopping: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self.consecutive_failures = 0
        self.rounds_started = 0
        self.rounds_completed = 0
        self.busy_refusals = 0
        self.deferred_gig = 0
        self.deferred_deck_playing = 0
        self.deferred_pressure_shed = 0
        self.halted_reason: str | None = None

    # ----- one round (worker thread) --------------------------------------

    def run_round(self, hub_url: str, name: str | None) -> RoundOutcome:
        """Run one journaled sync, unless one is already in flight."""
        reason = refuse_sync_round(
            self._data_dir,
            self._ui_mirror_provider(),
            force=False,
            pressure_payload=self._pressure_reader(),
        )
        if reason is not None:
            sync_status.journal_deferred(self._data_dir, reason)
            if reason == DEFER_REASON_GIG:
                self.deferred_gig += 1
            elif reason == DEFER_REASON_PRESSURE_SHED:
                self.deferred_pressure_shed += 1
                mark_scheduler_owed(self._data_dir)
            else:
                self.deferred_deck_playing += 1
            self._next_due = time.monotonic() + self._cfg.INTERVAL_S
            return "deferred"
        if not self._round_lock.acquire(blocking=False):
            self.busy_refusals += 1
            return "busy"
        try:
            self.rounds_started += 1
            outcome = self._sync_once(hub_url, name)
            if outcome in ("ok", "inconclusive"):
                clear_scheduler_owed(self._data_dir)
            return outcome
        finally:
            self.rounds_completed += 1
            self._next_due = time.monotonic() + next_delay_s(
                self._cfg.INTERVAL_S, self.consecutive_failures, self._cfg.MAX_BACKOFF_S
            )
            self._round_lock.release()

    def _sync_once(self, hub_url: str, name: str | None) -> RoundOutcome:
        try:
            result = self._sync_fn(self._data_dir, hub_url, name)
        except sync_client.SyncDigestMismatch as exc:
            self.halted_reason = f"digests diverged after settling: {exc}"
            log.error("cloudsync scheduler HALTED, no repair attempted (ADR 04 c6): %s", exc)
            return "halted"
        except SyncDeferredError as exc:
            # maintenance.sync's cross-process flock (single_flight.
            # sync_flock_for) is invisible to run_round's own in-process
            # self._round_lock check above, so a standalone CLI holding that
            # flock at just the wrong moment reaches here (Codex review, PR
            # #3831, P2/NON-BLOCKING). SyncDeferredError's own contract is "a
            # sync round was refused before any hub I/O" -- exactly what
            # "busy" already means elsewhere in this class -- so this must
            # not fall into the generic `except Exception` below and grow
            # the failure backoff over an expected skip, not a failure.
            self.busy_refusals += 1
            log.info("cloudsync round skipped (deferred: %s)", exc.reason)
            return "busy"
        except Exception:
            # Not swallowed: maintenance.sync journaled the error row that
            # status shows, and the backoff below is the declared response.
            self.consecutive_failures += 1
            log.exception("cloudsync round failed (%d in a row)", self.consecutive_failures)
            return "error"
        self.consecutive_failures = 0
        return "inconclusive" if result.digest_inconclusive else "ok"

    # ----- the loop (event loop) ------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("CloudSyncScheduler.start() called twice")
        self._stopping = asyncio.Event()
        self._next_due = time.monotonic() + self._cfg.INITIAL_DELAY_S
        self._task = asyncio.create_task(self._loop(), name="cloudsync-scheduler")
        self._task.add_done_callback(_log_loop_death)

    async def stop(self) -> None:
        """Stop waking, wait for an in-flight round, and clear the heartbeat."""
        if self._task is None or self._stopping is None:
            return
        self._stopping.set()
        try:
            await self._task
        finally:
            self._task = None
            sync_heartbeat.clear(self._data_dir)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _effective(self) -> sync_config.EffectiveConfig | None:
        try:
            effective = sync_config.resolve_config(self._data_dir, env=self._env)
        except sync_config.CloudSyncConfigError as exc:
            if str(exc) != self._last_config_error:
                log.error("cloudsync scheduler idle: %s", exc)
            self._last_config_error = str(exc)
            return None
        self._last_config_error = None
        return effective

    async def _loop(self) -> None:
        in_flight: asyncio.Task[RoundOutcome] | None = None
        try:
            while not self._stopped():
                effective = self._effective()
                if effective is None or not effective.configured or self.halted_reason:
                    sync_heartbeat.clear(self._data_dir)
                else:
                    hub_url = str(effective.hub_url)
                    sync_heartbeat.beat(self._data_dir, hub_url=hub_url)
                    if in_flight is not None and in_flight.done():
                        in_flight.result()
                        in_flight = None
                    if scheduler_owed(self._data_dir):
                        wake_reason = refuse_sync_round(
                            self._data_dir,
                            self._ui_mirror_provider(),
                            pressure_payload=self._pressure_reader(),
                        )
                        if wake_reason is None:
                            self._next_due = time.monotonic()
                    if in_flight is None and time.monotonic() >= self._next_due:
                        in_flight = asyncio.create_task(
                            asyncio.to_thread(self.run_round, hub_url, effective.machine_name)
                        )
                await self._sleep(self._cfg.BEAT_INTERVAL_S)
        finally:
            if in_flight is not None:
                await self._drain(in_flight)

    def _stopped(self) -> bool:
        return self._stopping is not None and self._stopping.is_set()

    async def _sleep(self, seconds: float) -> None:
        assert self._stopping is not None
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def _drain(self, in_flight: asyncio.Task[RoundOutcome]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(in_flight), timeout=self._cfg.STOP_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "cloudsync round still in flight after %ss; its thread finishes on its own",
                self._cfg.STOP_TIMEOUT_S,
            )


def _log_loop_death(task: asyncio.Task[None]) -> None:
    """A loop that dies on an exception must say so, not just stop beating."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("cloudsync scheduler loop died", exc_info=error)


@asynccontextmanager
async def scheduler_lifespan(
    data_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    ui_mirror_provider: UiMirrorProvider | None = None,
) -> AsyncIterator[CloudSyncScheduler | None]:
    """Run the scheduler for the life of an app; ``None`` on the hub.

    Started whenever this process is not the hub. While the config is off it
    only sleeps, so turning CloudSync on later needs no restart.
    """
    if machine_identity.is_hub_from_env(None if env is None else dict(env)):
        log.info("cloudsync scheduler not started: this process is the hub")
        yield None
        return
    scheduler = CloudSyncScheduler(
        data_dir, env=env, ui_mirror_provider=ui_mirror_provider
    )
    await scheduler.start()
    try:
        yield scheduler
    finally:
        await scheduler.stop()


__all__ = [
    "CFG",
    "CloudSyncScheduler",
    "RoundOutcome",
    "SchedulerCfg",
    "UiMirrorProvider",
    "next_delay_s",
    "scheduler_lifespan",
]
