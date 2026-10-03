"""Sentry quota guard: what may leave the machine, per error id and per day.

Sentry bills per event, not per issue. Team and Business both include 50k
errors a month -- about 1,667 a day across EVERY reporting host -- and one
preview Mac produced 1,374 in a day (Sun 13 Sep 2026). So each process gets:

- at most SENTRY_EVENTS_PER_ID_PER_HOUR events per error id per rolling hour,
  enough for Sentry to count hosts and keep first/last seen current, and
- at most SENTRY_EVENTS_PER_DAY events per UTC day, whatever the ids.

Perf-event console lines are mirrors: ``perf-event-log.ts`` decides what
escalates and posts the real report as ``ui-error``, so the console copy never
leaves. Only the REMOTE send is budgeted: the local JSONL sink and the client
daily log still record every event, and a suppression is logged once per
id-window and once when the daily cap trips, so a quiet Sentry is never
mistaken for a healthy app.

The limits come from replaying a real sink (``scripts/sentry_budget_replay.py``);
change them only with a new round in ``specs/sentry-quota-spec.md``.

Stdlib only, like the sink, so it imports without sentry-sdk.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SENTRY_EVENTS_PER_ID_PER_HOUR: int = 3
SENTRY_EVENTS_PER_DAY: int = 100
PERF_MIRROR_KINDS: frozenset[str] = frozenset({"console-warn", "console-error"})
PERF_EVENT_MARKER: str = "[perf-event]"
#: Vite dev-server console lines: they exist only under `vite dev` (the preview),
#: never in a shipped build, and were 12 of 33 open issues on Mon 14 Sep 2026.
DEV_TOOLING_MARKERS: tuple[str, ...] = ("[hmr]", "[vite]")
#: Exact browser fetch/abort messages. Safari WebKit reports ``Load failed``
#: (Sentry OPEN-DJ-FE-F); Chromium reports ``Failed to fetch``. Keep this set
#: in lockstep with ``telemetry-network-noise.ts``. Do not substring-match:
#: ``Failed to fetch dynamically imported module`` is a missing SPA chunk.
TRANSIENT_NETWORK_MESSAGES: frozenset[str] = frozenset(
    {
        "Load failed",
        "Failed to fetch",
        "NetworkError when attempting to fetch resource.",
        "The user aborted a request.",
        "The operation was aborted.",
    }
)
_TRANSIENT_NETWORK_PREFIXES: tuple[str, ...] = (
    "TypeError: ",
    "NetworkError: ",
    "AbortError: ",
    "DOMException: ",
)

_WINDOW_S: float = 3600.0


def is_perf_console_mirror(kind: str, message: str) -> bool:
    """A console line written by perf-event-log.ts; its real report is a ui-error."""
    return kind in PERF_MIRROR_KINDS and PERF_EVENT_MARKER in message


def is_dev_tooling_console(kind: str, message: str) -> bool:
    """A Vite dev-server console line (hot reload, reconnect): local only."""
    return kind in PERF_MIRROR_KINDS and message.lstrip().startswith(DEV_TOOLING_MARKERS)


def is_transient_network_error(_name: str | None, message: str) -> bool:
    """A browser fetch/abort TypeError: connectivity, not an application bug.

    Exact messages only so a missing dynamically imported module still ships.
    Callers pass the exception type plus message; matching is on the message,
    including a ``TypeError: Load failed`` combined form.
    """
    text = message.strip()
    if not text:
        return False
    if text in TRANSIENT_NETWORK_MESSAGES:
        return True
    for prefix in _TRANSIENT_NETWORK_PREFIXES:
        if text.startswith(prefix) and text[len(prefix) :] in TRANSIENT_NETWORK_MESSAGES:
            return True
    return False


def is_local_only_browser_error(kind: str, name: str | None, message: str) -> bool:
    """True when the event is recorded locally and must not leave for Sentry."""
    return (
        is_perf_console_mirror(kind, message)
        or is_dev_tooling_console(kind, message)
        or is_transient_network_error(name, message)
    )


@dataclass
class SentryBudget:
    """Per-process send budget. ``clock`` is injected so replays and tests can drive it."""

    per_id_per_hour: int
    per_day: int
    clock: Callable[[], float] = time.time
    suppressed: int = 0
    _recent: dict[str, deque[float]] = field(default_factory=dict)
    _muted: set[str] = field(default_factory=set)
    _day: str = ""
    _day_count: int = 0
    _day_capped: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, error_id: str) -> bool:
        """True when this event may be sent; False, counted and logged when over budget."""
        with self._lock:
            now = self.clock()
            self._roll_day(now)
            window = self._recent.setdefault(error_id, deque())
            while window and now - window[0] >= _WINDOW_S:
                window.popleft()
            if self._day_count >= self.per_day:
                if not self._day_capped:
                    self._day_capped = True
                    log.warning(
                        "sentry budget: daily cap of %d reached; events stay local "
                        "until %s ends (UTC)",
                        self.per_day,
                        self._day,
                    )
                self.suppressed += 1
                return False
            if len(window) >= self.per_id_per_hour:
                if error_id not in self._muted:
                    self._muted.add(error_id)
                    log.info(
                        "sentry budget: %s hit %d/hour; repeats stay local",
                        error_id,
                        self.per_id_per_hour,
                    )
                self.suppressed += 1
                return False
            self._muted.discard(error_id)
            window.append(now)
            self._day_count += 1
            return True

    def reset(self) -> None:
        with self._lock:
            self.suppressed = 0
            self._recent.clear()
            self._muted.clear()
            self._day = ""
            self._day_count = 0
            self._day_capped = False

    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day == self._day:
            return
        self._day, self._day_count, self._day_capped = day, 0, False
        # Bound memory: drop ids whose last send left the window.
        self._recent = {
            error_id: window
            for error_id, window in self._recent.items()
            if window and now - window[-1] < _WINDOW_S
        }


SENTRY_BUDGET = SentryBudget(
    per_id_per_hour=SENTRY_EVENTS_PER_ID_PER_HOUR,
    per_day=SENTRY_EVENTS_PER_DAY,
)
