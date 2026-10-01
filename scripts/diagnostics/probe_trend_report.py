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
import urllib.error
import urllib.request
import uuid
from typing import Any

from .probe_app_signals import ENGINE_OBSERVATION_TIMEOUT_SECONDS, fetch_json

CLIENT_ERROR_ACCEPTED_STATUS = 202


class TrendReportError(RuntimeError):
    """The RED trend could not be written to the engine's durable sink.

    It formats its own message from the url and the detail so that every
    raise site is one short call and the wording cannot drift between them.
    """

    def __init__(self, url: str, detail: str) -> None:
        super().__init__(f"POST {url} could not record the RED trend: {detail}")


def report_red_trend(port: int, trend: dict[str, Any]) -> dict[str, Any]:
    """Use the engine's durable client-error sink for a RED probe trend.

    Returns the accepted receipt, or raises TrendReportError naming the
    underlying error. It never returns a falsy value for a failure: the caller
    has to either see a receipt or handle an exception.
    """

    base_url = f"http://127.0.0.1:{port}/api/v1"
    health_url = base_url + "/health"
    try:
        health = fetch_json(health_url, ENGINE_OBSERVATION_TIMEOUT_SECONDS)
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

    payload = {
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
    url = base_url + "/client-errors"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=ENGINE_OBSERVATION_TIMEOUT_SECONDS) as response:
            status = int(response.status)
            receipt = json.load(response)
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
