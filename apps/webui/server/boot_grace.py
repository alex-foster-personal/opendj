"""Boot grace: the engine's background scanners wait for the launch's library index (PERF-BOOT-01).

Measured Tue 6 Oct 2026 on nucbox (curl only, 9,713-row copy): ``GET /tracks/index``
took 22 to 26 s right after the engine booted against 5.2 s cold once it had idled,
because three background threads ran their first pass at the same moment, each
GIL-heavy and interleaved with file I/O: the coverage drain's snapshot
(``webui.coverage-drain``), the reconcile summary scan started at startup
(``coverage-refresh``, HEALTH-15) and the path availability refresher
(``path-availability-refresh``). With the coverage drain off the index took 6 to 11 s.

So each of them holds its first pass until the first ``/tracks/index`` has been
served, or ``BOOT_GRACE_S`` has passed, whichever comes first. Nothing extends the
grace: a headless engine may never serve the index. The ahead-analysis drain has its
own 120 s grace (PERF-DRAIN-02), ended by the same index.

Only the daemon entry point arms a grace (``apps.webui.server.app``); an app built by
``create_app`` alone, as in tests, has none and its scanners start at once.

Requirements (mini-PRD):
  ✔︎ no first pass inside the grace
    [if] the grace is armed and no index was served [then] active() is True and wait() blocks
  ✔︎ the index ends it early
    [if] end() is called [then] active() is False at once and every waiter returns
  ✔︎ it never strands work
    [if] BOOT_GRACE_S passes with no index [then] active() is False and wait() returns
    [if] the scanner is stopping [then] wait() returns without waiting out the grace
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

#: The longest any scanner waits for the launch's library index.
BOOT_GRACE_S: float = 60.0
#: How often a waiter re-reads the clock and its stop flag.
WAIT_POLL_S: float = 0.5


class BootGrace:
    def __init__(self, grace_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._ends_at = clock() + grace_s
        self._over = threading.Event()
        if grace_s <= 0:
            self._over.set()

    def end(self) -> None:
        """The first library index went out: the grace is over (idempotent)."""
        self._over.set()

    def active(self) -> bool:
        if not self._over.is_set() and self._clock() >= self._ends_at:
            self._over.set()
        return not self._over.is_set()

    def wait(self, stop: threading.Event | None = None) -> None:
        """Block until the grace is over, or ``stop`` is set."""
        while self.active():
            if stop is not None and stop.is_set():
                return
            self._over.wait(WAIT_POLL_S)


#: No grace at all: what an app the daemon did not arm gets.
NO_GRACE = BootGrace(0.0)


def for_app(app: Any) -> BootGrace:
    """The app's armed grace, or ``NO_GRACE``."""
    grace = getattr(app.state, "boot_grace", None)
    return grace if isinstance(grace, BootGrace) else NO_GRACE


def arm(app: Any) -> BootGrace:
    """Arm the daemon's grace, starting now."""
    app.state.boot_grace = BootGrace(BOOT_GRACE_S)
    return app.state.boot_grace


__all__ = ["BOOT_GRACE_S", "NO_GRACE", "WAIT_POLL_S", "BootGrace", "arm", "for_app"]
