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
change them only with a new round in ``(private path)``.

Stdlib only, like the sink, so it imports without sentry-sdk.
"""

from __future__ import annotations

import logging
import re
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
#: Expected WebMIDI grant refusal / missing API. The panel already shows
#: lastError; console.error from older clients was OPEN-DJ-BE-6R.
MIDI_PERMISSION_CONSOLE_MARKERS: tuple[str, ...] = (
    "WebMIDI permission denied",
    "WebMIDI is not supported in this browser",
)
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
#: Newer WebKit appends the host it could not reach: ``Load failed (127.0.0.1:7700)``
#: (Sentry OPEN-DJ-BE-4G). Anchored, so only that exact shape matches.
_WEBKIT_LOAD_FAILED_HOST: re.Pattern[str] = re.compile(r"Load failed \([^()\s]+\)")
#: STATE-14 HTTP 503 code, as forwarded by the SPA ``RbApiError`` (Sentry
#: OPEN-DJ-FE-P / sibling OPEN-DJ-BE-7B). Token match only so a bare sqlite
#: ``database is locked`` still ships. Keep in lockstep with
#: ``telemetry-network-noise.ts``.
STATE_STORE_BUSY_TOKEN: str = "STATE_STORE_BUSY"
_TOKEN_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_]+")

#: uvicorn ERROR when SIGTERM hits still-open HTTP/WS tasks after
#: timeout_graceful_shutdown (INSTALL-33). The cancel is the bound working;
#: Sentry OPEN-DJ-BE-8X treated it as a crash.
UVICORN_GRACEFUL_SHUTDOWN_MARKER: str = "timeout graceful shutdown exceeded"
#: The only site and exact shape that is suppressed: uvicorn's own
#: ``logger.error("Cancel %s running task(s), timeout graceful shutdown exceeded")``.
UVICORN_GRACEFUL_SHUTDOWN_SOURCE_SITE: str = "engine:uvicorn.error"
_UVICORN_GRACEFUL_SHUTDOWN_MESSAGE = re.compile(
    r"Cancel [0-9]+ running task\(s\), " + re.escape(UVICORN_GRACEFUL_SHUTDOWN_MARKER)
)

#: Expected deck-load refusals a DJ already sees as a toast/banner
#: (Sentry OPEN-DJ-BE-8H / OPEN-DJ-BE-7S). Token plus colon so a mention
#: in a real crash still ships. Keep in lockstep with
#: ``isExpectedLibraryRefusalMessage`` in ``deck-load-failure-copy.ts``.
#: AUDIO_FILE_MISSING is deliberately absent: it also covers iCloud stubs
#: and unopenable local files.
EXPECTED_LIBRARY_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "CLOUD_ASSET_UNAVAILABLE",
        "AUDIO_NOT_ON_THIS_MACHINE",
        "CLOUD_POLICY_UNCONFIGURED",
        "CLOUD_HYDRATING",
        "TRACK_NOT_FOUND",
        "USB_STICK_NOT_MOUNTED",
        "USB_FILE_MISSING",
        "USB_TRACK_NOT_FOUND",
    }
)
_EXPECTED_LIBRARY_REFUSAL_TOKEN: re.Pattern[str] = re.compile(
    r"(?:^|[\s:])(" + "|".join(sorted(EXPECTED_LIBRARY_REFUSAL_CODES)) + r"):"
)
#: Banner/toast sentences after CLOUDSYNC-33/35 rewrite the raw code away.
_EXPECTED_LIBRARY_REFUSAL_SENTENCES: tuple[str, ...] = (
    "This file isn't on this computer.",
    "This track's audio is not on this machine and could not be fetched.",
    "Its audio isn't on this Mac (looked at ",
)

_WINDOW_S: float = 3600.0


def is_perf_console_mirror(kind: str, message: str) -> bool:
    """A console line written by perf-event-log.ts; its real report is a ui-error."""
    return kind in PERF_MIRROR_KINDS and PERF_EVENT_MARKER in message


def is_dev_tooling_console(kind: str, message: str) -> bool:
    """A Vite dev-server console line (hot reload, reconnect): local only."""
    return kind in PERF_MIRROR_KINDS and message.lstrip().startswith(DEV_TOOLING_MARKERS)


def is_expected_midi_permission_console(kind: str, message: str) -> bool:
    """A console line for WebMIDI denial or missing WebMIDI: UI already shows it."""
    if kind not in PERF_MIRROR_KINDS:
        return False
    return any(marker in message for marker in MIDI_PERMISSION_CONSOLE_MARKERS)


def is_transient_network_error(_name: str | None, message: str) -> bool:
    """A browser fetch/abort TypeError: connectivity, not an application bug.

    Exact messages only so a missing dynamically imported module still ships.
    Callers pass the exception type plus message; matching is on the message,
    including a ``TypeError: Load failed`` combined form.
    """
    text = message.strip()
    if not text:
        return False
    if text in TRANSIENT_NETWORK_MESSAGES or _WEBKIT_LOAD_FAILED_HOST.fullmatch(text):
        return True
    for prefix in _TRANSIENT_NETWORK_PREFIXES:
        rest = text[len(prefix) :]
        if text.startswith(prefix) and (
            rest in TRANSIENT_NETWORK_MESSAGES or _WEBKIT_LOAD_FAILED_HOST.fullmatch(rest)
        ):
            return True
    return False


#: Engine availability worker (LIBM-09): after SQLITE_BUSY retries it logs at
#: ERROR so the local JSONL sink still records the round, then stays queued
#: and drains. That is designed contention, not a crash (Sentry OPEN-DJ-BE-7V).
AVAILABILITY_LOCK_GIVE_UP_MARKER: str = (
    "database lock retries; unchecked rows stay queued"
)


def is_availability_lock_give_up(kind: str, message: str) -> bool:
    """True for the engine worker's lock-retry give-up log (OBS-5412)."""
    return kind == "engine" and AVAILABILITY_LOCK_GIVE_UP_MARKER in message


def is_expected_library_refusal(message: str) -> bool:
    """A known play refusal the DJ already saw, not an application crash."""
    if _EXPECTED_LIBRARY_REFUSAL_TOKEN.search(message) is not None:
        return True
    return any(sentence in message for sentence in _EXPECTED_LIBRARY_REFUSAL_SENTENCES)


def is_expected_state_store_busy(_name: str | None, message: str) -> bool:
    """STATE-14 HTTP 503 forwarded as a browser RbApiError (OBS-5454).

    Token-matches ``STATE_STORE_BUSY`` in the exception name or message.
    A bare sqlite ``database is locked`` is a different fault and still ships.
    """
    for text in (_name, message):
        if not text:
            continue
        if STATE_STORE_BUSY_TOKEN in _TOKEN_RE.findall(text):
            return True
    return False


def is_local_only_browser_error(kind: str, name: str | None, message: str) -> bool:
    """True when the event is recorded locally and must not leave for Sentry."""
    return (
        is_perf_console_mirror(kind, message)
        or is_dev_tooling_console(kind, message)
        or is_expected_midi_permission_console(kind, message)
        or is_transient_network_error(name, message)
        or is_expected_library_refusal(message)
        or is_expected_state_store_busy(name, message)
    )


def is_uvicorn_graceful_shutdown_timeout(kind: str, source_site: str, message: str) -> bool:
    """uvicorn ERROR after the INSTALL-33 graceful window: local sink only.

    Matches only the engine's ``uvicorn.error`` logger and uvicorn's complete
    message, so another error that merely mentions the phrase still ships.
    """
    return (
        kind == "engine"
        and source_site == UVICORN_GRACEFUL_SHUTDOWN_SOURCE_SITE
        and _UVICORN_GRACEFUL_SHUTDOWN_MESSAGE.fullmatch(message) is not None
    )


#: uvicorn HTTP/WS protocols log this first line with ``exc_info`` when an
#: unhandled ASGI callable raises (``httptools_impl.run_asgi``). ``warning_log``
#: forwards the ERROR; Sentry OPEN-DJ-BE-8Z grouped every such fault under the
#: wrapper because ``maybe_send_sentry`` used ``capture_message`` of the log.
UVICORN_ASGI_EXCEPTION_PREFIX: str = "Exception in ASGI application"


def is_uvicorn_asgi_exception_log(message: str) -> bool:
    """True when the log is uvicorn's unhandled-ASGI wrapper, not the inner fault."""
    return message.lstrip().startswith(UVICORN_ASGI_EXCEPTION_PREFIX)


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
