"""Generic single-flight background scan engine.

Supersedes: the ScanRunner class bodies in audio_quality_scan.py and
grid_quality_scan.py (issue #7646). Domain modules keep a thin adapter
named ScanRunner so routes, tests, and grid_audio_fit_scan stay put.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Generic, Literal, TypeVar, cast

log = logging.getLogger(__name__)

TResult = TypeVar("TResult")


class ScanRunner(Generic[TResult]):
    def __init__(
        self,
        *,
        thread_name: str,
        label: str,
        on_busy: Literal["refuse", "wait"] = "wait",
        extra_status: Callable[[bool, threading.Event, Any], dict[str, Any]] | None = None,
        on_result: Callable[[TResult], None] | None = None,
        shutdown_join_s: float = 5.0,
    ) -> None:
        self._thread_name = thread_name
        self._label = label
        self._on_busy = on_busy
        self._extra_status = extra_status
        self._on_result = on_result
        self._shutdown_join_s = shutdown_join_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop = threading.Event()
        self._extra: Any = None
        self._last_result: TResult | None = None
        self._last_error: str | None = None

    def _claim(self, extra: Any) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._extra = extra
            self._stop = threading.Event()
            return True

    def _run(self, work: Callable[[threading.Event], TResult]) -> None:
        stop = self._stop
        result: TResult | None = None
        error: str | None = None
        try:
            result = work(stop)
        except Exception as exc:
            log.exception("%s scan failed", self._label)
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._running = False
            self._extra = None
            self._last_error = error
            if result is not None:
                self._last_result = result
        if result is not None and self._on_result is not None:
            self._on_result(result)

    def status(self) -> dict[str, Any]:
        with self._lock:
            extra = self._extra_status(self._running, self._stop, self._extra) if self._extra_status else {}
            return {
                "state": "running" if self._running else "idle",
                "last_result": asdict(cast(Any, self._last_result)) if self._last_result is not None else None,
                "last_error": self._last_error,
                **(extra or {}),
            }

    def start_work(
        self,
        work: Callable[[threading.Event], TResult],
        extra: Any = None,
    ) -> dict[str, Any]:
        if self._claim(extra):
            thread = threading.Thread(
                target=self._run,
                args=(work,),
                name=self._thread_name,
                daemon=True,
            )
            with self._lock:
                self._thread = thread
            thread.start()
        return self.status()

    def run_work(
        self,
        work: Callable[[threading.Event], TResult],
        extra: Any = None,
    ) -> dict[str, Any]:
        if self._on_busy == "refuse":
            if not self._claim(extra):
                return self.status()
            self._run(work)
            return self.status()
        while not self._claim(extra):
            self.join(timeout_s=600)
            time.sleep(0.05)
        self._run(work)
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        return self.status()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(self._shutdown_join_s)
            if thread.is_alive():
                log.warning(
                    "%s scan still stopping after %s s at shutdown",
                    self._label,
                    self._shutdown_join_s,
                )

    def join(self, timeout_s: float) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError(f"{self._label} scan still running after {timeout_s} s")
