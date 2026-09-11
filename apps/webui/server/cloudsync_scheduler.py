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
running. A failed tick is logged at ERROR and journaled by the sync itself
(so ``GET /cloudsync/status`` and ``GET /feedback/sync/status`` both show it);
the loop keeps going because the next tick is how an offline machine catches
up.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from apps.sync_hub import status as sync_status

from .routes.feedback_sync import (
    FeedbackSyncError,
    FeedbackSyncOut,
    store_for_app,
    sync_feedback_pins,
)

log = logging.getLogger(__name__)

#: CFG. How often a configured engine syncs. A pin dropped on one machine
#: shows on another within about two ticks (one push, one pull).
CLOUDSYNC_INTERVAL_S: float = 60.0


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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        # The switch is read BEFORE the layout is checked: an unconfigured app
        # (every test app, most dev boots) must not be refused over a data-dir
        # layout it will never sync. A configured one with a split layout
        # raises here, at boot, rather than on the first tick.
        data_dir = Path(str(self._app.state.state_db_path)).resolve().parent.parent
        link = sync_status.read_status(data_dir, env=self._env)
        if not link.enabled:
            log.info("CloudSync scheduler not running: %s", link.reason)
            return
        store_for_app(self._app)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cloudsync-scheduler", daemon=True
        )
        self._thread.start()
        log.info(
            "CloudSync scheduler running every %.0fs against %s",
            self._interval_s, link.endpoint,
        )

    def run_once(self) -> FeedbackSyncOut | None:
        """One tick. Returns the result, or None after logging why it failed."""
        try:
            return sync_feedback_pins(store_for_app(self._app))
        except FeedbackSyncError as exc:
            log.error("CloudSync scheduled sync failed [%s]: %s", exc.code, exc)
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self._interval_s)


__all__ = ["CLOUDSYNC_INTERVAL_S", "CloudSyncScheduler"]
