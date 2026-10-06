"""Daemon drain for user-ordered stems and lyrics jobs (issue #1865).

Two supervisors, one per lane, one running item each. Off the request
thread. ``create_app`` leaves this OFF so pytest does not spawn workers.

``MUSIC_DJ_LIBRARY_JOBS`` is a fail-fast enum, ``on`` or ``off``, default on
for the real daemon.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apps.analysis import queue_user, queue_user_runner
from apps.analysis.queue_user_lanes import USER_JOB_LANES
from apps.analysis.queue_user_runner import runner_from_environ
from apps.analysis.store import open_conn
from apps.shared.state.db import is_sqlite_busy

log = logging.getLogger(__name__)

LIBRARY_JOBS_ENV: str = "MUSIC_DJ_LIBRARY_JOBS"
LIBRARY_JOBS_VALUES: tuple[str, ...] = ("on", "off")
DEFAULT_INTERVAL_S: float = 1.0
THREAD_NAME: str = "webui.library-jobs"
RESTART_JOIN_S: float = 5.0


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    raw = environ.get(LIBRARY_JOBS_ENV, "on").strip().lower()
    if raw not in LIBRARY_JOBS_VALUES:
        raise ValueError(
            f"{LIBRARY_JOBS_ENV}={raw!r} is not a member of {LIBRARY_JOBS_VALUES}"
        )
    return raw == "on"


@dataclass
class LibraryJobsState:
    enabled: bool
    interval_s: float = DEFAULT_INTERVAL_S
    ticks: int = 0


def build(*, enabled: bool, interval_s: float = DEFAULT_INTERVAL_S) -> LibraryJobsState:
    return LibraryJobsState(enabled=enabled, interval_s=interval_s)


class LibraryJobsWatcher:
    def __init__(
        self,
        *,
        state_db: Path,
        stems_root: Path,
        data_dir: Path,
        enabled: bool,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self.state_db = state_db
        self.stems_root = stems_root
        self.data_dir = data_dir
        self.enabled = enabled
        self.interval_s = interval_s
        runner_from_environ()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Ticks skipped because the writer lock stayed busy past busy_timeout.
        self.busy_ticks = 0

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=THREAD_NAME, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=RESTART_JOIN_S)
        self._thread = None

    def tick(self) -> None:
        if not self.enabled:
            return
        conn = open_conn(self.state_db)
        try:
            queue_user.ensure_user_schema(conn)
            for lane in USER_JOB_LANES:
                queue_user_runner.tick_lane(
                    conn,
                    lane,
                    runner_id=f"libjobs-{lane}-{os.getpid()}",
                    stems_root=self.stems_root,
                    data_dir=self.data_dir,
                )
        finally:
            conn.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.run_tick()

    def run_tick(self) -> None:
        """One loop pass: a tick that lost the writer lock is retried next tick.

        The tick's connection already waits ``busy_timeout``
        (:data:`apps.shared.state.db.DEFAULT_BUSY_TIMEOUT_S`, via ``open_rw``),
        so reaching here busy means another writer held the lock past it. That
        is contention, not a defect in this drain: one WARNING line with a
        running count, no traceback (STATE-22). Every other error keeps the
        full ``log.exception`` it always had, and the loop keeps running.
        """
        try:
            self.tick()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_busy(exc):
                log.exception("library-jobs tick failed")
                return
            self.busy_ticks += 1
            log.warning(
                "library-jobs tick skipped: state.db writer lock busy past "
                "busy_timeout (%d busy tick(s) since start), retrying next tick: %s",
                self.busy_ticks,
                exc,
            )
        except Exception:
            log.exception("library-jobs tick failed")
