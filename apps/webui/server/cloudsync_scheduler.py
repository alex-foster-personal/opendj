"""The engine's CloudSync scheduler (FBSYNC-01: pins arrive with no manual step).

CSSTATUS-01 defined the switch (``MDT_CLOUDSYNC_SCHEDULER=1`` plus
``MDT_CLOUDSYNC_HUB_URL``) and reported ``enabled: false`` because nothing ran
on it. This is the thing that runs: while the switch is on, every
:data:`CLOUDSYNC_INTERVAL_S` seconds it calls
:func:`apps.webui.server.routes.feedback_sync.sync_feedback_pins`, the same
function behind ``POST /api/v1/feedback/sync`` and the CLI. One tick is a full
CloudSync round trip of the whole state DB (that is what CloudSync is), book-
ended by the feedback pin reconcile.

Off unless configured, and it says so: ``start()`` logs the reason it is not
running, and a switch value other than ''/0/1 is a WARNING, not a quiet off.

It never takes the engine down (PR #1978 review). A scheduler that cannot
start (a split data-dir layout, an unreadable journal) logs at ERROR, journals
an ``error`` result and stays off; the engine boots and plays regardless. A
tick that fails in any way, expected or not, is logged, recorded and, when
unexpected, journaled with its traceback in the log; the loop keeps going,
because the next tick is how an offline machine catches up.
``GET /feedback/sync/status`` reports all of this as ``scheduler``.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from apps.shared.events import publish
from apps.sync_hub import status as sync_status
from apps.sync_hub.scheduler import next_delay_s
from apps.sync_hub.transport import classify_transport_failure

from .routes.feedback import _now
from .routes.feedback_sync import (
    CloudSyncSchedulerOut,
    FeedbackSyncError,
    FeedbackSyncOut,
    SchedulerState,
    store_for_app,
    sync_feedback_pins,
)

log = logging.getLogger(__name__)

#: CFG. How often a configured engine syncs. A pin dropped on one machine
#: shows on another within about two ticks (one push, one pull).
CLOUDSYNC_INTERVAL_S: float = 60.0

#: While wire versions differ, retries cannot succeed until a binary upgrade.
WIRE_MISMATCH_BASE_S: float = 300.0
WIRE_MISMATCH_MAX_BACKOFF_S: float = 3600.0
HUB_ERROR_BASE_S: float = 300.0
HUB_ERROR_MAX_BACKOFF_S: float = 3600.0

#: The only values the switch admits; anything else is a misconfiguration.
_SWITCH_VALUES: tuple[str, ...] = ("", "0", "1")


def announce_pull(app: FastAPI, result: FeedbackSyncOut) -> None:
    """Tell clients and the coverage drain that a sync brought rows in.

    A pull can add tracks and locations, which moves the health lights'
    denominator. Nothing is announced for a round that pulled nothing.
    """
    if result.pulled <= 0:
        return
    publish("library.changed", {"kind": "tracks", "ids": []})
    drain = getattr(app.state, "coverage_drain", None)
    if drain is not None:
        drain.wake()


class CloudSyncScheduler:
    """One background thread per app; inert unless the switch is on."""

    def __init__(
        self,
        app: FastAPI,
        *,
        interval_s: float = CLOUDSYNC_INTERVAL_S,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._app = app
        self._interval_s = interval_s
        self._env = os.environ if env is None else env
        self._data_dir = Path(str(app.state.state_db_path)).resolve().parent.parent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: SchedulerState = "off"
        self._reason: str | None = "not started"
        self._ticks = 0
        self._last_ok_at: str | None = None
        self._last_error: str | None = None
        self._last_error_at: str | None = None
        self._wire_mismatch_failures = 0
        self._hub_error_failures = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> CloudSyncSchedulerOut:
        state: SchedulerState = self._state
        if state == "running" and not self.running:
            state = "dead"
        return CloudSyncSchedulerOut(
            state=state,
            alive=self.running,
            reason=self._reason,
            ticks=self._ticks,
            last_ok_at=self._last_ok_at,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
        )

    def start(self) -> None:
        switch = self._env.get(sync_status.SCHEDULER_ENV, "")
        if switch not in _SWITCH_VALUES:
            self._state = "misconfigured"
            self._reason = (
                f"{sync_status.SCHEDULER_ENV}={switch!r} is not 0 or 1, so the "
                f"CloudSync scheduler stays off"
            )
            log.warning("CloudSync scheduler not running: %s", self._reason)
            return
        # The switch is read BEFORE the layout is checked: an unconfigured app
        # (every test app, most dev boots) must not be refused over a data-dir
        # layout it will never sync.
        try:
            link = sync_status.read_status(self._data_dir, env=self._env)
            # ``enabled`` needs a fresh heartbeat (evidence a loop is alive).
            # ``configured`` is intent only; using ``enabled`` here would refuse
            # to start before the first beat exists (CAT-04, PR #1998).
            if link.configured:
                store_for_app(self._app)
        except (FeedbackSyncError, sync_status.CloudSyncStatusError) as exc:
            self._refuse_to_start(exc)
            return
        if not link.configured:
            self._state, self._reason = "off", link.reason
            log.info("CloudSync scheduler not running: %s", link.reason)
            return
        self._stop.clear()
        self._state, self._reason = "running", None
        self._thread = threading.Thread(
            target=self._loop, name="cloudsync-scheduler", daemon=True
        )
        self._thread.start()
        log.info(
            "CloudSync scheduler running every %.0fs against %s",
            self._interval_s, link.endpoint,
        )

    def run_once(self) -> FeedbackSyncOut | None:
        """One tick. Returns the result, or None after logging and recording why not."""
        self._ticks += 1
        try:
            result = sync_feedback_pins(store_for_app(self._app))
        except FeedbackSyncError as exc:
            # maintenance.sync has already journaled a failed round trip.
            log.error("CloudSync scheduled sync failed [%s]: %s", exc.code, exc)
            self._record_error(f"[{exc.code}] {exc}")
            return None
        except Exception as exc:
            # Deliberately broad: the loop must outlive any one tick, and the
            # failure is logged with its traceback, recorded and journaled.
            log.exception("CloudSync scheduled sync crashed; the scheduler keeps ticking")
            message = f"{type(exc).__name__}: {exc}"
            self._record_error(message)
            self._journal_error(f"scheduled feedback pin sync crashed: {message}")
            return None
        self._last_ok_at = _now()
        self._wire_mismatch_failures = 0
        self._hub_error_failures = 0
        announce_pull(self._app, result)
        return result

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
            self._thread = None
            self._state, self._reason = "stopped", "stopped with the engine"

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            delay_s = self._next_wait_s()
            self._stop.wait(delay_s)

    def _next_wait_s(self) -> float:
        try:
            current = sync_status.read_status(self._data_dir, env=self._env)
        except sync_status.CloudSyncStatusError:
            return self._interval_s
        if current.update_required is not None:
            self._hub_error_failures = 0
            self._wire_mismatch_failures += 1
            return next_delay_s(
                WIRE_MISMATCH_BASE_S,
                self._wire_mismatch_failures - 1,
                WIRE_MISMATCH_MAX_BACKOFF_S,
            )
        if self._is_hub_transport_failure(current):
            self._wire_mismatch_failures = 0
            self._hub_error_failures += 1
            return next_delay_s(
                HUB_ERROR_BASE_S,
                self._hub_error_failures - 1,
                HUB_ERROR_MAX_BACKOFF_S,
            )
        self._wire_mismatch_failures = 0
        self._hub_error_failures = 0
        return self._interval_s

    def _is_hub_transport_failure(self, current: sync_status.CloudSyncStatus) -> bool:
        last = current.last_result
        if last is None or last.get("status") != "error":
            return False
        message = last.get("message")
        if not isinstance(message, str):
            return False
        return classify_transport_failure(message) is not None

    def _refuse_to_start(self, exc: Exception) -> None:
        code = getattr(exc, "code", type(exc).__name__)
        self._state = "misconfigured"
        self._reason = f"[{code}] {exc}"
        log.error(
            "CloudSync scheduler not started, the engine runs without it: %s", self._reason
        )
        self._record_error(self._reason)
        self._journal_error(f"CloudSync scheduler not started: {self._reason}")

    def _record_error(self, message: str) -> None:
        self._last_error, self._last_error_at = message, _now()

    def _journal_error(self, message: str) -> None:
        """Put the failure where ``GET /cloudsync/status`` and the chip read it."""
        try:
            sync_status.write_result(
                self._data_dir,
                sync_status.SyncResult(
                    finished_at=_now(), status="error", message=message, pushed=0, pulled=0,
                ),
            )
        except sync_status.CloudSyncStatusError:
            log.exception("could not journal the CloudSync scheduler failure: %s", message)


__all__ = [
    "CLOUDSYNC_INTERVAL_S",
    "CloudSyncScheduler",
    "HUB_ERROR_BASE_S",
    "HUB_ERROR_MAX_BACKOFF_S",
    "WIRE_MISMATCH_BASE_S",
    "WIRE_MISMATCH_MAX_BACKOFF_S",
]
