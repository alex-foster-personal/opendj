"""Daemon drain for user-ordered stems and lyrics jobs (issue #1865).

Two supervisors, one per lane, one running item each. Off the request
thread. ``create_app`` leaves this OFF so pytest does not spawn workers.

``MUSIC_DJ_LIBRARY_JOBS`` is a fail-fast enum, ``on`` or ``off``, default on
for the real daemon.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apps.analysis import queue_user, queue_user_runner
from apps.analysis.queue_user_lanes import USER_JOB_LANES
from apps.analysis.store import open_conn

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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
            try:
                self.tick()
            except Exception:
                log.exception("library-jobs tick failed")
