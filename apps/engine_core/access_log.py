"""Rate-limit uvicorn access logging to prevent disk exhaustion."""

from __future__ import annotations

import logging
import time
from collections import deque

ACCESS_LOG_MAX_LINES_PER_SECOND = 50
ACCESS_LOG_SUSTAINED_SECONDS = 60

_MUTE_LOGGER = "apps.engine_core.access_log"


class AccessLogRateGate:
    def __init__(
        self,
        *,
        max_lines_per_second: int = ACCESS_LOG_MAX_LINES_PER_SECOND,
        window_seconds: int = ACCESS_LOG_SUSTAINED_SECONDS,
    ) -> None:
        self.max_lines_per_second = max_lines_per_second
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._muted = False

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def allow(self, now: float | None = None) -> bool:
        if now is None:
            now = time.monotonic()
        self._prune(now)
        threshold = self.max_lines_per_second * self.window_seconds
        if len(self._timestamps) >= threshold:
            if not self._muted:
                self._muted = True
                rate = len(self._timestamps) / self.window_seconds
                logging.getLogger(_MUTE_LOGGER).warning(
                    "uvicorn access logging muted: %.1f lines/s over %ds "
                    "(threshold %d/s)",
                    rate,
                    self.window_seconds,
                    self.max_lines_per_second,
                )
            return False
        if self._muted:
            self._muted = False
        self._timestamps.append(now)
        return True


class AccessLogRateFilter(logging.Filter):
    def __init__(self, gate: AccessLogRateGate | None = None) -> None:
        super().__init__()
        self.gate = gate or AccessLogRateGate()

    def filter(self, _record: logging.LogRecord) -> bool:
        return self.gate.allow()


def configure_access_log_sampling() -> None:
    gate = AccessLogRateGate()
    logging.getLogger("uvicorn.access").addFilter(AccessLogRateFilter(gate))
