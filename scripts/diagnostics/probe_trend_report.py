"""Write a RED probe trend to the engine's durable client-error sink.

Split out of ``probe_app_signals`` along the seam its own docstring named:
everything there is a best-effort OBSERVATION, where losing one leaves a
sample with a missing field and the probe keeps producing footprints. Posting
a RED trend is the opposite. It is the durable RECORD of a failure the caller
has already decided is real, and a swallowed failure here means nothing
anywhere says the app regressed.

So this module never returns a falsy value for a failure: the caller sees a
receipt or handles a :class:`TrendReportError`.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from .probe_app_signals import fetch_json

CLIENT_ERROR_ACCEPTED_STATUS = 202
TREND_REPORT_BUDGET_SECONDS = 30.0
"""Wall-clock budget for ONE RED-trend report: the identity check plus the POST.

This used to be 1.5 s per request, the sampler's number, and it lost the report
exactly when it mattered. The first write to a new daily client-error log
creates the file and then restricts it to its owner, which on Windows is two
subprocesses run AFTER the row is written; on a loaded host that alone outlasts
1.5 s (Windows gate run 36286250497: ``POST .../client-errors could not record
the RED trend: TimeoutError: timed out``). A RED trend is what an overloaded
machine produces, so the report was slowest precisely when it was needed.

The caller is ``trend``, a diagnostics command run by an agent or an operator.
It is on no user-latency path, so waiting costs nothing next to an unrecorded
RED. A dead engine does not wait this long: a refused connection fails at once.
"""
TREND_REPORT_ATTEMPTS = 1
"""How many times the report is sent: once. A longer wait, never a retry.

The engine's client-error route does not dedupe on ``client_event_id``: every
POST appends a row under a fresh ``event_id``, so two POSTs carrying one id are
two rows. A LATE reply is the common case here, not a lost request - the row is
already on disk while the client is still waiting to hear about it - so a retry
after a timeout would record the same RED trend twice. Retrying needs an
idempotent sink first; until the engine dedupes, this stays 1.
"""
_SOCKET_TIMEOUT_GRACE_SECONDS = 1.0
"""How far the socket's own timeout sits BEHIND the report deadline.

The deadline is enforced by the wait in ``_within_report_budget``, the only
place that decides the budget is spent. The socket timeout exists just so an
abandoned worker ends by itself; set to the same instant, either one could fire
first and the same event would be reported two different ways.
"""


class TrendReportError(RuntimeError):
    """The RED trend could not be written to the engine's durable sink.

    It formats its own message from the url and the detail so that every
    raise site is one short call and the wording cannot drift between them.
    """

    def __init__(self, url: str, detail: str) -> None:
        super().__init__(f"POST {url} could not record the RED trend: {detail}")


class _ReportBudgetSpent(Exception):
    """The report deadline passed while a request was still unanswered."""


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), json.load(response)


def _within_report_budget(deadline: float, exchange: Callable[[float], Any]) -> Any:
    """Run one HTTP exchange and stop WAITING for it at ``deadline``.

    A socket timeout alone is not a bound on the wait: it restarts on every
    read, so an engine that starts its reply and then sends a byte now and
    again holds the caller for as long as it likes. The exchange therefore
    runs in a worker and the caller waits for the worker, on the monotonic
    clock, until the deadline and no longer.

    The worker's own failure is re-raised here, in the caller, unchanged. A
    worker abandoned at the deadline is a daemon thread: it ends at its socket
    timeout, or with the process.
    """

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ReportBudgetSpent
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = exchange(remaining + _SOCKET_TIMEOUT_GRACE_SECONDS)
        except Exception as exc:  # relayed to the caller below, never swallowed
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="opendj-trend-report", daemon=True)
    worker.start()
    worker.join(remaining)
    if worker.is_alive():
        raise _ReportBudgetSpent
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _budget_spent_detail(started: float, budget_seconds: float) -> str:
    """How long the report waited and how many times it was sent, in words."""

    waited = time.monotonic() - started
    plural = "" if TREND_REPORT_ATTEMPTS == 1 else "s"
    return (
        f"no reply after waiting {waited:.1f} s (budget {budget_seconds:.1f} s, "
        f"{TREND_REPORT_ATTEMPTS} attempt{plural})"
    )


def _trend_payload(trend: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_event_id": f"performance-trend-{uuid.uuid4().hex}",
        "kind": "ui-error",
        "message": f"performance probe RED: {trend.get('verdict_reason', 'unknown reason')}",
        "name": "OpenDJPerformanceTrend",
        "url": "opendj-performance-probe://trend",
        "client_timestamp": str(trend.get("window", {}).get("last", "")),
        "user_agent": "opendj-performance-probe",
        "secure_context": True,
        "audio_worklet_available": False,
        "context": {
            "source": "performance-probe",
            "verdict": "RED",
            "orphan_count": int(trend.get("orphan_count", 0)),
        },
    }


def _require_engine_identity(health_url: str, started: float, budget_seconds: float) -> None:
    """Refuse to post until whatever answers on the port says it is the engine."""

    deadline = started + budget_seconds
    try:
        health = _within_report_budget(deadline, lambda timeout: fetch_json(health_url, timeout))
    except _ReportBudgetSpent as exc:
        raise TrendReportError(
            health_url,
            "engine identity check failed: "
            + _budget_spent_detail(started, budget_seconds)
            + "; nothing was posted",
        ) from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise TrendReportError(
            health_url,
            f"engine identity check failed: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(health, dict) or health.get("status") != "ok" or not isinstance(
        health.get("version"), str
    ):
        raise TrendReportError(
            health_url, "engine identity check returned an invalid health record"
        )


def report_red_trend(
    port: int,
    trend: dict[str, Any],
    *,
    budget_seconds: float = TREND_REPORT_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Use the engine's durable client-error sink for a RED probe trend.

    Returns the accepted receipt, or raises TrendReportError naming the
    underlying error.

    The identity check and the POST share ONE wall-clock budget, and each is
    sent once (``TREND_REPORT_ATTEMPTS``). A reply that has not finished when
    the budget is spent raises a TrendReportError saying how long it waited and
    how many attempts it made; for the POST it also says the engine may still
    record the trend, because a request that was sent and went unanswered is
    not known to be lost.
    """

    if budget_seconds <= 0:
        raise ValueError(f"budget_seconds must be positive, got {budget_seconds!r}")
    started = time.monotonic()
    base_url = f"http://127.0.0.1:{port}/api/v1"
    _require_engine_identity(base_url + "/health", started, budget_seconds)

    payload = _trend_payload(trend)
    url = base_url + "/client-errors"
    try:
        status, receipt = _within_report_budget(
            started + budget_seconds, lambda timeout: _post_json(url, payload, timeout)
        )
    except _ReportBudgetSpent as exc:
        raise TrendReportError(
            url,
            _budget_spent_detail(started, budget_seconds)
            + "; the POST was sent and is not retried, because the engine does not "
            "dedupe on client_event_id and a second POST could record this trend "
            f"twice, so the engine may still record {payload['client_event_id']}",
        ) from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise TrendReportError(url, f"{type(exc).__name__}: {exc}") from exc
    if status != CLIENT_ERROR_ACCEPTED_STATUS:
        raise TrendReportError(
            url, f"HTTP {status}, expected {CLIENT_ERROR_ACCEPTED_STATUS}"
        )
    if not isinstance(receipt, dict) or not isinstance(receipt.get("event_id"), str):
        raise TrendReportError(url, "engine returned no client-error event id")
    if receipt.get("stored") is not True:
        raise TrendReportError(
            url, "engine accepted the request but did not persist the client-error record"
        )
    return {
        "posted": True,
        "http_status": status,
        "client_event_id": payload["client_event_id"],
        "engine_event_id": receipt["event_id"],
    }
