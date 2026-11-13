"""Local-first latency and count metrics (OBS-12).

WHY AGGREGATE HERE, NOT IN SENTRY

Every surface that does work (an HTTP route, a background loop, a job, a CLI
run, a subprocess, an outbound call, an engine command) records ONE sample per
completed unit into an in-process aggregator. A flush every
:data:`FLUSH_INTERVAL_S` turns each series into one compact summary row (count,
errors, p50/p95/max from a fixed bucket histogram) and:

1. appends it to a local JSONL file, always, when a log dir is configured.
   That file is the full record, the same role the error sink plays for errors,
   so with a log dir configured only rows the file took go on to step 2.
2. hands it to the installed :attr:`MetricsAggregator.forwarder`, only when
   telemetry consent is not held (``consent.held_for_consent``, OBS-05). Held
   summaries wait (bounded) and go out on the first flush after consent.
   No forwarder is installed by this module; with none, rows stay local.

Sending raw samples instead would bill one Sentry item per request: the UI
mirror alone PUTs once a second. Summaries cost at most
:data:`SENTRY_ITEMS_PER_SUMMARY` items per series per window, whatever the
traffic, so volume scales with the number of distinct surfaces in use, not
with how hard a test suite or a tester hammers them.

PRIVACY

Metric names and tag values are LABELS: a route template, a job kind, a
command name. Tag keys are an allowlist (:data:`ALLOWED_TAG_KEYS`); any other
key is dropped, and every value is passed through the shared path and token
scrubber and truncated. A track title has no tag to ride in.

COST OF RECORDING

``record`` takes one lock, does a dict lookup and a bisect, and allocates
nothing once a series exists. It is safe on a request path; it is NOT for an
audio callback (the Rust engine keeps its own atomics for that).

Stdlib only, like the sink: it imports without sentry-sdk.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.shared.telemetry.consent import held_for_consent
from apps.shared.telemetry.scrub import scrub_string

log = logging.getLogger(__name__)

#: Seconds between flushes of the in-process window.
FLUSH_INTERVAL_S: float = 300.0
#: Distinct series one window may hold; the rest fold into one overflow series
#: so a bug that mints names (an id in a name) cannot grow memory or the bill.
MAX_SERIES_PER_WINDOW: int = 400
#: Sentry items one summary costs at most: count, errors, p50, p95, max.
SENTRY_ITEMS_PER_SUMMARY: int = 5
#: Sentry metric items one process may send per UTC day. 400 series x 5 items x
#: 12 windows an hour would be 24k an hour; real use touches far fewer series,
#: and this cap is the backstop. Sentry Team includes 5 GB of metrics a month.
SENTRY_ITEMS_PER_DAY: int = 20_000
#: Summaries held back while a deck is live or consent is pending.
MAX_PENDING_SUMMARIES: int = 2_000
#: Local summary rows per UTC day; the file stops growing past this.
MAX_LOCAL_ROWS_PER_DAY: int = 50_000

#: Latency buckets in ms (upper bounds). Percentiles report a bucket's upper
#: bound, so p95=200 reads "95% finished within 200 ms".
BUCKETS_MS: tuple[float, ...] = (
    1,
    2,
    5,
    10,
    20,
    50,
    100,
    200,
    500,
    1_000,
    2_000,
    5_000,
    10_000,
    30_000,
    60_000,
    300_000,
    3_600_000,
)

#: Tag keys a metric may carry. Anything else is dropped at record time.
ALLOWED_TAG_KEYS: frozenset[str] = frozenset(
    {
        "channel",  # electron ipc channel
        "command",  # engine command type
        "kind",  # job kind, loop name, event kind
        "lane",  # analysis lane
        "library",  # outbound http library (httpx, requests, urllib3)
        "method",  # http method
        "origin",  # browser, shell, engine, rust-engine
        "outcome",  # ok, error, timeout, cancelled
        "program",  # subprocess program basename
        "route",  # route template, never a concrete path
        "status",  # status class: 2xx, 4xx, 5xx
        "target",  # outbound host (no path, no query)
    }
)
MAX_TAG_VALUE_CHARS: int = 80
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
OVERFLOW_SERIES: str = "telemetry.series_overflow"
LOG_DIR_ENV: str = "OPENDJ_METRICS_LOG_DIR"


def _clean_tags(tags: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Keep allowlisted tag keys only, with each value scrubbed and truncated."""
    if not tags:
        return ()
    cleaned: list[tuple[str, str]] = []
    for key, value in tags.items():
        if key not in ALLOWED_TAG_KEYS or value is None:
            continue
        text = scrub_string(str(value))[:MAX_TAG_VALUE_CHARS]
        cleaned.append((key, text))
    cleaned.sort()
    return tuple(cleaned)


@dataclass
class _Series:
    """One series' running count, errors and fixed-bucket latency histogram."""

    count: int = 0
    errors: int = 0
    timed: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))

    def add(self, value_ms: float | None, error: bool) -> None:
        """Count one sample, timing it when ``value_ms`` is given."""
        self.count += 1
        if error:
            self.errors += 1
        if value_ms is None:
            return
        value = max(0.0, float(value_ms))
        self.timed += 1
        self.total_ms += value
        self.max_ms = max(self.max_ms, value)
        self.buckets[bisect.bisect_left(BUCKETS_MS, value)] += 1

    def percentile(self, q: float) -> float | None:
        """The upper bound of the bucket holding quantile ``q``, or None untimed."""
        if self.timed == 0:
            return None
        rank = q * self.timed
        seen = 0
        for index, hits in enumerate(self.buckets):
            seen += hits
            if seen >= rank and hits:
                return BUCKETS_MS[index] if index < len(BUCKETS_MS) else self.max_ms
        return self.max_ms


def summary_row(
    name: str, tags: tuple[tuple[str, str], ...], series: _Series, window: tuple[float, float]
) -> dict[str, Any]:
    """One flushed series as a plain dict (the local JSONL row and the forward)."""
    row: dict[str, Any] = {
        "name": name,
        "tags": dict(tags),
        "count": series.count,
        "errors": series.errors,
        "window_start": round(window[0], 3),
        "window_end": round(window[1], 3),
    }
    if series.timed:
        row["p50_ms"] = series.percentile(0.50)
        row["p95_ms"] = series.percentile(0.95)
        row["max_ms"] = round(series.max_ms, 3)
        row["mean_ms"] = round(series.total_ms / series.timed, 3)
    return row


Forwarder = Callable[[list[dict[str, Any]]], bool]


def _valid_summary(name: str, count: int, errors: int, buckets: list[int] | None) -> bool:
    """A merged summary's shape: a legal name, sane counts, the shared bucket layout."""
    if not NAME_RE.match(name) or count < 0 or errors < 0 or errors > count:
        return False
    return buckets is None or (len(buckets) == len(BUCKETS_MS) + 1 and all(b >= 0 for b in buckets))


class MetricsAggregator:
    """Thread-safe windowed aggregator. ``clock`` is injected for tests."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        """Start an empty window at ``clock()`` with no log dir and no forwarder."""
        self._clock = clock
        self._lock = threading.Lock()
        self._series: dict[tuple[str, tuple[tuple[str, str], ...]], _Series] = {}
        self._window_start = clock()
        self._pending: deque[dict[str, Any]] = deque(maxlen=MAX_PENDING_SUMMARIES)
        self.dropped_pending = 0
        self.rejected_names = 0
        self.log_dir: Path | None = None
        self._local_day = ""
        self._local_rows = 0
        self.forwarder: Forwarder | None = None

    # ----- recording -----------------------------------------------------
    def record(
        self,
        name: str,
        value_ms: float | None = None,
        *,
        tags: Mapping[str, Any] | None = None,
        error: bool = False,
    ) -> None:
        """Count one completed unit; ``value_ms`` is its duration when timed."""
        if not NAME_RE.match(name):
            self.rejected_names += 1
            return
        key = (name, _clean_tags(tags))
        with self._lock:
            series = self._series.get(key)
            if series is None:
                if len(self._series) >= MAX_SERIES_PER_WINDOW:
                    key = (OVERFLOW_SERIES, ())
                    series = self._series.setdefault(key, _Series())
                else:
                    series = self._series[key] = _Series()
            series.add(value_ms, error)

    def merge(
        self,
        name: str,
        *,
        tags: Mapping[str, Any] | None = None,
        count: int,
        errors: int = 0,
        buckets: list[int] | None = None,
        total_ms: float = 0.0,
        max_ms: float = 0.0,
    ) -> bool:
        """Fold a summary aggregated elsewhere (the Rust engine) into this window.

        ``buckets`` must use :data:`BUCKETS_MS` plus one overflow bucket, the
        layout the engine's ``telemetry.rs`` mirrors. Returns False, and
        records nothing, for a malformed summary rather than guessing.
        """
        if not _valid_summary(name, count, errors, buckets):
            self.rejected_names += 1
            return False
        key = (name, _clean_tags(tags))
        with self._lock:
            series = self._series.get(key)
            if series is None:
                if len(self._series) >= MAX_SERIES_PER_WINDOW:
                    key = (OVERFLOW_SERIES, ())
                    series = self._series.setdefault(key, _Series())
                else:
                    series = self._series[key] = _Series()
            series.count += count
            series.errors += errors
            if buckets is not None:
                timed = sum(buckets)
                series.timed += timed
                series.total_ms += max(0.0, float(total_ms))
                series.max_ms = max(series.max_ms, float(max_ms))
                for index, hits in enumerate(buckets):
                    series.buckets[index] += hits
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        """The current window, unflushed, sorted by name. Read-only."""
        with self._lock:
            window = (self._window_start, self._clock())
            rows = [summary_row(n, t, s, window) for (n, t), s in self._series.items()]
        return sorted(rows, key=lambda row: (row["name"], sorted(row["tags"].items())))

    # ----- flushing ------------------------------------------------------
    def flush(self) -> list[dict[str, Any]]:
        """Close the window: write rows locally, try to forward, return the rows."""
        with self._lock:
            now = self._clock()
            window = (self._window_start, now)
            rows = [summary_row(n, t, s, window) for (n, t), s in self._series.items()]
            self._series = {}
            self._window_start = now
        # Local first: with a log dir configured, only rows the local file
        # holds may go on to Sentry; a row it could not take is dropped.
        self._forward(self._write_local(rows, now) if rows else rows)
        return rows

    def _forward(self, rows: list[dict[str, Any]]) -> None:
        """Queue rows and hand the queue to the forwarder unless consent is held."""
        for row in rows:
            if len(self._pending) == self._pending.maxlen:
                self.dropped_pending += 1
            self._pending.append(row)
        forwarder = self.forwarder
        if forwarder is None or not self._pending:
            if forwarder is None:
                self._pending.clear()
            return
        if held_for_consent():
            # OBS-05: nothing leaves the machine until the terms were accepted.
            return
        batch = list(self._pending)
        try:
            sent = forwarder(batch)
        except Exception:
            log.warning("metrics forward failed; summaries stay local", exc_info=True)
            sent = False
        if sent:
            self._pending.clear()

    def _write_local(self, rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Append rows to today's file; return the rows that may be forwarded.

        With no log dir there is no local record to keep, so every row may go.
        Otherwise only the rows actually written: none when the directory is
        unwritable, and none past the day's cap.
        """
        log_dir = self.log_dir
        if log_dir is None:
            return rows
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self._local_day:
            self._local_day, self._local_rows = day, 0
        room = MAX_LOCAL_ROWS_PER_DAY - self._local_rows
        if room <= 0:
            return []
        rows = rows[:room]
        written = 0
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"telemetry-metrics-{day}.jsonl"
            # newline="\n": the same bytes on Windows as on macOS and Linux.
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    written += 1
        except OSError:
            # A buffered write is only confirmed when the file closes cleanly,
            # so none of this batch counts as held locally.
            log.warning("metrics: could not append to %s", log_dir, exc_info=True)
            self._local_rows += written
            return []
        self._local_rows += written
        return rows

    def reset_for_tests(self) -> None:
        """Return the aggregator to its just-constructed state."""
        with self._lock:
            self._series = {}
            self._window_start = self._clock()
        self._pending.clear()
        self.dropped_pending = 0
        self.rejected_names = 0
        self.log_dir = None
        self.forwarder = None
        self._local_day, self._local_rows = "", 0


METRICS = MetricsAggregator()


def record(
    name: str,
    value_ms: float | None = None,
    *,
    tags: Mapping[str, Any] | None = None,
    error: bool = False,
) -> None:
    """Module-level shorthand for :meth:`MetricsAggregator.record` on :data:`METRICS`."""
    METRICS.record(name, value_ms, tags=tags, error=error)


# ----- the flusher thread ---------------------------------------------------
class _Flusher:
    """The process's one flush thread and its stop event."""

    __slots__ = ("stop", "thread")

    def __init__(self) -> None:
        """No thread yet; the stop event starts clear."""
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


_FLUSHER = _Flusher()


def configure_metrics(log_dir: Path | None) -> None:
    """Point local summaries at ``log_dir`` (``OPENDJ_METRICS_LOG_DIR`` wins)."""
    override = os.environ.get(LOG_DIR_ENV, "").strip()
    METRICS.log_dir = Path(override) if override else log_dir


def start_metrics_flusher(interval_s: float = FLUSH_INTERVAL_S) -> bool:
    """Start the daemon flush loop once per process. Returns whether it started."""
    if _FLUSHER.thread is not None and _FLUSHER.thread.is_alive():
        return False
    _FLUSHER.stop.clear()

    def _loop() -> None:
        """Flush every ``interval_s`` until stopped."""
        while not _FLUSHER.stop.wait(interval_s):
            try:
                METRICS.flush()
            except Exception:
                log.warning("metrics flush failed", exc_info=True)

    thread = threading.Thread(target=_loop, name="opendj-metrics-flush", daemon=True)
    _FLUSHER.thread = thread
    thread.start()
    return True


def stop_metrics_flusher(*, final_flush: bool = True) -> None:
    """Stop the loop; flush what the window holds so a clean exit loses nothing."""
    _FLUSHER.stop.set()
    thread = _FLUSHER.thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    _FLUSHER.thread = None
    if final_flush:
        try:
            METRICS.flush()
        except Exception:
            log.warning("final metrics flush failed", exc_info=True)


__all__ = [
    "ALLOWED_TAG_KEYS",
    "BUCKETS_MS",
    "FLUSH_INTERVAL_S",
    "LOG_DIR_ENV",
    "MAX_SERIES_PER_WINDOW",
    "METRICS",
    "OVERFLOW_SERIES",
    "SENTRY_ITEMS_PER_DAY",
    "SENTRY_ITEMS_PER_SUMMARY",
    "MetricsAggregator",
    "configure_metrics",
    "record",
    "start_metrics_flusher",
    "stop_metrics_flusher",
    "summary_row",
]
