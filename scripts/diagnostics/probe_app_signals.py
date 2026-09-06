"""In-app signals the OS counters cannot see: engine HTTP and the browser ring.

Both are best-effort by design. A probe that dies because the engine is down,
or because a WebKit LocalStorage file is mid-write, would stop producing the
process footprints that are its actual job.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .probe_types import ProcessRow

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
PERF_RING_STORAGE_KEY = "mdt.perfEventLog"
DECK_LOAD_COMPLETED_KIND = "deck-load"
CLIENT_ERROR_ACCEPTED_STATUS = 202


def _fetch_json(url: str, timeout: float = 1.5) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _job_rollup(jobs: list[Any]) -> dict[str, Any]:
    """Counts plus the still-running jobs, so a sample explains its own CPU."""

    statuses = Counter(
        str(item.get("status", "unknown")) for item in jobs if isinstance(item, dict)
    )
    return {
        "count": len(jobs),
        "by_status": dict(statuses),
        "active": [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "worker_pid": item.get("worker_pid"),
            }
            for item in jobs
            if isinstance(item, dict) and item.get("status") not in TERMINAL_JOB_STATUSES
        ],
    }


def engine_metrics(family: list[tuple[ProcessRow, str]]) -> dict[str, Any]:
    engine = next((row for row, role in family if role == "python-engine"), None)
    if engine is None:
        return {"available": False, "reason": "python engine not found"}
    port_match = re.search(r"(?:^|\s)--port\s+(\d+)(?:\s|$)", engine.command)
    if port_match is None:
        return {"available": False, "reason": "engine port not found", "pid": engine.pid}
    port = int(port_match.group(1))
    base = f"http://127.0.0.1:{port}/api/v1"
    result: dict[str, Any] = {"available": True, "pid": engine.pid, "port": port}
    for name, path in (
        ("health", "/health"),
        ("jobs", "/jobs"),
        ("clients", "/telemetry/clients"),
    ):
        try:
            value = _fetch_json(base + path)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result[name] = {"error": type(exc).__name__}
            continue
        result[name] = _job_rollup(value) if name == "jobs" and isinstance(value, list) else value
    return result


class TrendReportError(RuntimeError):
    """The RED trend could not be written to the engine's durable sink.

    It formats its own message from the url and the detail so that every
    raise site is one short call and the wording cannot drift between them.

    This is deliberately NOT best-effort like the rest of this module. The
    other readers here are observations: losing one leaves a sample with a
    missing field and the probe keeps producing footprints. Posting a RED
    trend is the opposite - it is the durable RECORD of a failure that the
    caller has already decided is real, and a swallowed failure there means
    nothing anywhere says the app regressed.
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
        health = _fetch_json(health_url)
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
        with urllib.request.urlopen(request, timeout=1.5) as response:
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


def _is_completed_deck_load(kind: str) -> bool:
    """True only for a load that actually FINISHED.

    audio-engine.svelte.ts writes `deck-load sid=<id>` once the deck can play,
    and `deck-load-fail` from the load catch block. A `deck-load` PREFIX test
    therefore reads a failure as a load, so four failed attempts would
    fabricate a four-deck loaded state. Allowlist the completion instead of
    denylisting the failure kinds that exist today: a `deck-load-*` kind added
    later is then ignored until it is named here, rather than silently read as
    a completed load.
    """

    return kind == DECK_LOAD_COMPLETED_KIND or kind.startswith(DECK_LOAD_COMPLETED_KIND + " ")


def _decode_local_storage_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return str(value)
    if len(value) >= 2 and value[1] == 0:
        return value.decode("utf-16-le")
    return value.decode("utf-8")


def _local_storage_candidates(bundle_ids: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for bundle_id in bundle_ids:
        root = Path.home() / "Library/WebKit" / bundle_id / "WebsiteData/Default"
        try:
            found.extend(root.glob("*/*/LocalStorage/localstorage.sqlite3"))
        except OSError:
            continue
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def _read_perf_ring(path: Path) -> list[Any] | None:
    """The perf event ring from one LocalStorage file, or None if unreadable."""

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (PERF_RING_STORAGE_KEY,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        events = json.loads(_decode_local_storage_value(row[0]))
    except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(events, list) or not events:
        return None
    return events


def _newest_perf_ring(bundle_ids: Iterable[str]) -> tuple[Path, list[Any]] | None:
    """Newest-write-wins across every candidate bundle id's storage file."""

    best: tuple[str, Path, list[Any]] | None = None
    for path in _local_storage_candidates(bundle_ids):
        events = _read_perf_ring(path)
        if events is None:
            continue
        last = events[-1]
        last_timestamp = str(last.get("t", "")) if isinstance(last, dict) else ""
        if best is None or last_timestamp > best[0]:
            best = (last_timestamp, path, events)
    return None if best is None else (best[1], best[2])


def browser_perf_ring(bundle_ids: Iterable[str]) -> dict[str, Any]:
    newest = _newest_perf_ring(bundle_ids)
    if newest is None:
        return {"available": False, "reason": "perf ring not found"}
    path, events = newest
    last = events[-1] if isinstance(events[-1], dict) else {}
    loads = [
        event
        for event in events
        if isinstance(event, dict) and str(event.get("kind", "")).startswith("deck-load")
    ]
    deck_state: dict[int, bool] = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("deck"), int):
            continue
        deck = event["deck"]
        kind = str(event.get("kind", ""))
        if _is_completed_deck_load(kind):
            deck_state[deck] = True
        elif kind in {"deck-state-empty", "deck-unload"}:
            deck_state[deck] = False
        # A failed or unrecognized load says nothing about what the deck holds,
        # so the deck keeps whatever state was last OBSERVED. A deck whose only
        # ring row is such an event stays absent from deck_state, which keeps
        # loaded_deck_count at None rather than inventing a count.
    return {
        "available": True,
        "storage_path": str(path),
        "event_count": len(events),
        "first_timestamp": events[0].get("t") if isinstance(events[0], dict) else None,
        "last_timestamp": last.get("t"),
        "last_kind": last.get("kind"),
        "deck_load_count_in_ring": len(loads),
        # A bounded ring can omit a deck's most recent state. Publishing a
        # made-up zero would turn missing evidence into a false clean unload.
        "loaded_deck_count": sum(deck_state.values()) if len(deck_state) == 4 else None,
        "last_deck_load": loads[-1] if loads else None,
    }
