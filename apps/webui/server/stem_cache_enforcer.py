"""Timer that keeps the stem cache inside its disk-aware budget (STEM-39).

:func:`apps.cloud.stem_cache_budget.enforce` already runs after every hydrate.
That alone is not enough: the disk fills for reasons that have nothing to do
with stems (a download, a build, another app), and a machine that hydrates
nothing for a day would never notice. This loop re-measures on an interval.

The engine's inputs to the decision are gathered in ONE place,
:func:`cache_inputs`, and shared by the timer and the HTTP routes, so the
route an agent calls and the tick the engine runs cannot disagree about which
directory, which index or which decks they mean.

A tick that raises is logged at ERROR and recorded in ``last_error`` for the
status route. The loop itself keeps running: a corrupt settings file must be
visible and fixable, not a reason for the engine to stop watching the disk.

* [if] the engine can hydrate and the disk is under the floor [then] a tick
  evicts R2-confirmed bundles down to the floor.
* [if] no hydration source is armed [then] a tick evicts nothing and the
  status names ``hydration_not_armed``.
* [if] a tick raises [then] ``last_error`` carries it and the next tick runs.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from apps.cloud import stem_cache_budget, stem_hydration, stem_index
from apps.shared.paths import DATA_DIR
from apps.stems.artifacts import DEFAULT_STEMS_DIR

log = logging.getLogger(__name__)

#: Delay before the first tick, so a boot does not start hashing bundles
#: while the engine is still warming up for its first deck load.
FIRST_TICK_DELAY_S: float = 60.0
THREAD_NAME: str = "opendj-stem-cache-enforcer"


@dataclass(frozen=True)
class CacheInputs:
    """Everything the engine contributes to one budget decision."""

    stems_dir: Path
    data_dir: Path
    index: stem_cache_budget.StemAssetIndex
    protected: frozenset[str]
    can_rehydrate: bool


def cache_data_dir(app: FastAPI) -> Path:
    """The data directory ``app`` was built on (``create_app`` records it),
    else the process default for a bare router-only app."""
    return Path(getattr(app.state, "stem_cache_data_dir", None) or DATA_DIR)


def cache_inputs(app: FastAPI) -> CacheInputs:
    """Resolve the stems directory, data directory, cached R2 index, the
    loaded-or-playing set and whether hydration is armed, for ``app``.

    The index is the LOCAL cache copy only (no network). A missing cache is
    an empty index, which makes every bundle local-only and therefore
    un-evictable: the safe reading of "we have not fetched the index yet".
    """
    data_dir = cache_data_dir(app)
    return CacheInputs(
        stems_dir=Path(getattr(app.state, "stems_dir", DEFAULT_STEMS_DIR)),
        data_dir=data_dir,
        index=stem_index.load_cached_index(data_dir),
        protected=stem_hydration.OPEN_DECKS.open_ids(),
        can_rehydrate=getattr(app.state, "stem_hydration_source", None) is not None,
    )


def enforce_for_app(app: FastAPI, *, dry_run: bool = False) -> stem_cache_budget.EnforceReport:
    inputs = cache_inputs(app)
    return stem_cache_budget.enforce(
        inputs.stems_dir,
        data_dir=inputs.data_dir,
        index=inputs.index,
        protected=inputs.protected,
        can_rehydrate=inputs.can_rehydrate,
        dry_run=dry_run,
    )


class StemCacheEnforcer:
    """Background loop: one :func:`enforce_for_app` per interval."""

    def __init__(self, app: FastAPI, *, first_tick_delay_s: float = FIRST_TICK_DELAY_S) -> None:
        self._app = app
        self._first_tick_delay_s = first_tick_delay_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_report: stem_cache_budget.EnforceReport | None = None
        self.last_error: str | None = None
        self.ticks: int = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tick(self) -> stem_cache_budget.EnforceReport | None:
        """One enforcement pass. Returns the report, or ``None`` when the
        pass raised (the error is in ``last_error`` and the log)."""
        self.ticks += 1
        try:
            report = enforce_for_app(self._app)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("stem-cache: enforcement tick failed")
            return None
        self.last_report = report
        self.last_error = None
        if report.evicted_stable_ids:
            log.warning(
                "stem-cache: low disk, evicted %d R2-confirmed bundle(s), freed %d bytes "
                "(free %d, floor %d)",
                len(report.evicted_stable_ids),
                report.bytes_freed,
                report.disk_free_bytes,
                report.floor_bytes,
            )
        elif report.blocked_reason is not None:
            log.warning(
                "stem-cache: low disk and nothing evicted [%s] (free %d, floor %d, "
                "%d bundle(s) queued for upload)",
                report.blocked_reason,
                report.disk_free_bytes,
                report.floor_bytes,
                len(report.queued_for_upload),
            )
        return report

    def _interval_s(self) -> float:
        try:
            return stem_cache_budget.load_settings(
                cache_data_dir(self._app)
            ).enforce_interval_s
        except stem_cache_budget.StemCacheSettingsError as exc:
            self.last_error = f"StemCacheSettingsError: {exc}"
            log.error("stem-cache: settings unreadable, using the default interval: %s", exc)
            return stem_cache_budget.DEFAULT_ENFORCE_INTERVAL_S

    def _run(self) -> None:
        if self._stop.wait(self._first_tick_delay_s):
            return
        while True:
            self.tick()
            if self._stop.wait(self._interval_s()):
                return

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


__all__ = [
    "FIRST_TICK_DELAY_S",
    "THREAD_NAME",
    "CacheInputs",
    "StemCacheEnforcer",
    "cache_data_dir",
    "cache_inputs",
    "enforce_for_app",
]
