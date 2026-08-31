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
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .probe_types import ProcessRow

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
PERF_RING_STORAGE_KEY = "mdt.perfEventLog"


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
        result[name] = (
            _job_rollup(value) if name == "jobs" and isinstance(value, list) else value
        )
    return result


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
    return {
        "available": True,
        "storage_path": str(path),
        "event_count": len(events),
        "first_timestamp": events[0].get("t") if isinstance(events[0], dict) else None,
        "last_timestamp": last.get("t"),
        "last_kind": last.get("kind"),
        "deck_load_count_in_ring": len(loads),
        "last_deck_load": loads[-1] if loads else None,
    }
