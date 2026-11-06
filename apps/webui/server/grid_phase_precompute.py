"""Idle kick-phase cache warming for Viva and OLTF playlists (GRID-PHASE-9154)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from apps.shared.env_on_off import read_on_off_environ

from . import boot_grace
from .coverage_drain_analysis import DeckGate
from .rb_vendor_pkg.grid_phase_overlay import idle_pass

log = logging.getLogger(__name__)

GRID_PHASE_PRECOMPUTE_ENV: str = "MUSIC_DJ_GRID_PHASE_PRECOMPUTE"
GRID_PHASE_PRECOMPUTE_VALUES: tuple[str, ...] = ("on", "off")
THREAD_NAME: str = "webui.grid-phase-precompute"
TICK_INTERVAL_S: float = 5.0
IDLE_INTERVAL_S: float = 60.0
STOP_JOIN_S: float = 5.0


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    return read_on_off_environ(
        environ,
        env_key=GRID_PHASE_PRECOMPUTE_ENV,
        allowed=GRID_PHASE_PRECOMPUTE_VALUES,
        default="on",
        normalize=True,
    )


@dataclass
class GridPhasePrecomputeStatus:
    state: str = "idle"
    reason: str | None = None
    ticks: int = 0
    processed: int = 0
    skipped: int = 0
    yielded: int = 0
    last_job: dict[str, Any] | None = None
    updated_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GridPhasePrecompute:
    def __init__(
        self,
        *,
        conn_factory,
        state_db_path: Path,
        playing_fn,
        grace: boot_grace.BootGrace = boot_grace.NO_GRACE,
        clock=time.time,
    ) -> None:
        self._conn_factory = conn_factory
        self._state_db_path = Path(state_db_path)
        self._playing_fn = playing_fn
        self._grace = grace
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = GridPhasePrecomputeStatus()
        self._tick_lock = threading.Lock()

    def status(self) -> GridPhasePrecomputeStatus:
        return self._status

    def tick(self) -> str:
        with self._tick_lock:
            result = idle_pass(
                conn_factory=self._conn_factory,
                state_db_path=self._state_db_path,
                playing_fn=self._playing_fn,
            )
            self._status.state = "yielded" if result.yielded else "idle"
            self._status.reason = "deck activity" if result.yielded else None
            self._status.ticks += 1
            self._status.processed += len(result.processed)
            self._status.skipped += len(result.skipped)
            self._status.yielded += int(result.yielded)
            self._status.last_job = {
                "processed": list(result.processed),
                "skipped": list(result.skipped),
                "yielded": result.yielded,
                "remaining": list(result.remaining),
            }
            self._status.updated_at = self._clock()
            return self._status.state

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=STOP_JOIN_S)
        self._thread = None

    def _run(self) -> None:
        self._grace.wait(self._stop)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("grid phase precompute tick failed")
                self._status.state = "blocked"
                self._status.reason = "idle precompute failed; see daemon log"
                self._status.updated_at = self._clock()
            interval_s = TICK_INTERVAL_S if self._status.state == "yielded" else IDLE_INTERVAL_S
            self._stop.wait(interval_s)


def build_for_app(app: Any) -> GridPhasePrecompute:
    from apps.shared.state import db as state_db

    db = Path(app.state.state_db_path)

    def mirror() -> Any:
        return getattr(app.state, "ui_mirror", None)

    return GridPhasePrecompute(
        conn_factory=lambda: state_db.open_ro(db),
        state_db_path=db,
        playing_fn=DeckGate(mirror),
        grace=boot_grace.for_app(app),
    )


__all__ = [
    "GRID_PHASE_PRECOMPUTE_ENV",
    "GRID_PHASE_PRECOMPUTE_VALUES",
    "IDLE_INTERVAL_S",
    "TICK_INTERVAL_S",
    "GridPhasePrecompute",
    "GridPhasePrecomputeStatus",
    "arm_from_environ",
    "build_for_app",
]
