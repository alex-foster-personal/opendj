"""Bounded client performance samples and latest native process footprint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path
from ..machine_pressure import read_machine_pressure

router = APIRouter(prefix="/performance/telemetry", tags=["performance-telemetry"])
log = logging.getLogger(__name__)

DEFAULT_PROCESS_LOG_DIRS = (
    Path.home() / "Library/Application Support/OpenDJ Diagnostics/performance",
    Path.home() / ".local/share/music-dj-tools/performance",
)
MAX_PROCESS_RECORD_BYTES = 512 * 1024
FRESH_PROCESS_SAMPLE_SECONDS = 45.0
# Allowlist, not a denylist: the probe record carries command lines and PIDs,
# and only these four aggregate numbers are safe to hand back to the browser.
SAFE_TOTAL_KEYS = frozenset(
    {
        "physical_footprint_mb",
        "summed_lifetime_peak_mb",
        "cpu_percent",
        "process_count",
    }
)


class DeckPerformanceSample(BaseModel):
    deck_id: Literal[1, 2, 3, 4]
    stable_id: str | None = Field(default=None, max_length=64)
    duration_ms: float | None = Field(default=None, ge=0)
    playing: bool
    audible: bool
    transport_pending: bool
    stem_status: Literal["unavailable", "ready", "error"]
    last_load_latency_ms: float | None = Field(default=None, ge=0)
    sync_error: str | None = Field(default=None, max_length=2048)
    processor_error: str | None = Field(default=None, max_length=2048)


class ClientPerformanceSampleIn(BaseModel):
    client_sample_id: str = Field(min_length=1, max_length=128)
    client_session_id: str = Field(min_length=1, max_length=128)
    client_timestamp: str = Field(min_length=1, max_length=128)
    route: str = Field(min_length=1, max_length=2048)
    page_uptime_ms: float = Field(ge=0)
    js_heap_mb: float | None = Field(default=None, ge=0)
    pcm_estimated_mb: float = Field(ge=0)
    anlz_estimated_mb: float = Field(ge=0)
    anlz_entry_count: int = Field(ge=0)
    prefetch_mb: float = Field(ge=0)
    prefetch_count: int = Field(ge=0)
    audio_health_hz: float | None = Field(default=None, ge=0)
    audio_health_level: Literal["idle", "ok", "warn", "crit"]
    perf_event_count: int = Field(ge=0)
    decks: list[DeckPerformanceSample] = Field(min_length=4, max_length=4)


class ClientPerformanceSampleOut(BaseModel):
    event_id: str
    stored: bool


def _received_at() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@router.post("/client-samples", response_model=ClientPerformanceSampleOut, status_code=202)
def capture_client_performance(
    payload: ClientPerformanceSampleIn, request: Request
) -> ClientPerformanceSampleOut:
    """Persist one compact semantic sample without blocking the audio path."""

    event_id = uuid.uuid4().hex[:16]
    record = {
        "schema_version": 1,
        "kind": "client-performance-sample",
        "event_id": event_id,
        "received_at": _received_at(),
        **payload.model_dump(),
    }
    log_dir = Path(
        getattr(request.app.state, "performance_log_dir", DEFAULT_LOG_DIR)
    )
    path = daily_log_path(log_dir, "webui-performance", time.gmtime())
    stored = append_json_record(path, record)
    if not stored:
        log.warning("performance sample %s not stored: daily cap reached at %s", event_id, path)
    return ClientPerformanceSampleOut(event_id=event_id, stored=stored)


def _read_last_json_line(path: Path) -> dict[str, object] | None:
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        with path.open("rb") as handle:
            handle.seek(max(0, size - MAX_PROCESS_RECORD_BYTES))
            block = handle.read(MAX_PROCESS_RECORD_BYTES)
    except OSError:
        return None
    lines = [line for line in block.splitlines() if line.strip()]
    for raw in reversed(lines):
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("kind") == "sample":
            return value
    return None


def _latest_process_record(log_dirs: tuple[Path, ...]) -> dict[str, object] | None:
    files: list[Path] = []
    for directory in log_dirs:
        try:
            files.extend(directory.glob("opendj-performance-*.jsonl"))
        except OSError:
            continue
    for path in sorted(files, key=lambda item: item.stat().st_mtime, reverse=True):
        record = _read_last_json_line(path)
        if record is not None:
            return record
    return None


def _timestamp_age_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _footprint_by_role(processes: object) -> dict[str, float]:
    """Per-role megabytes only: the command strings never leave the probe log."""

    by_role: dict[str, float] = {}
    if not isinstance(processes, list):
        return by_role
    for process in processes:
        if not isinstance(process, dict):
            continue
        role = process.get("role")
        footprint = process.get("physical_footprint_mb")
        if isinstance(role, str) and isinstance(footprint, (int, float)):
            by_role[role] = round(by_role.get(role, 0.0) + float(footprint), 1)
    return by_role


def _safe_totals(totals: object) -> dict[str, float]:
    """Allowlisted numeric totals, so a probe schema change cannot widen this."""

    if not isinstance(totals, dict):
        return {}
    return {
        key: value
        for key, value in totals.items()
        if key in SAFE_TOTAL_KEYS and isinstance(value, (int, float))
    }


def _kernel_pressure_level(machine: object) -> int | None:
    if not isinstance(machine, dict):
        return None
    level = machine.get("kernel_memory_pressure_level")
    return level if isinstance(level, int) else None


@router.get("/processes")
def latest_process_telemetry(request: Request) -> dict[str, object]:
    """Return a privacy-reduced Activity Monitor-style app breakdown."""

    configured = getattr(
        request.app.state, "performance_process_log_dirs", DEFAULT_PROCESS_LOG_DIRS
    )
    log_dirs = tuple(Path(directory) for directory in configured)
    record = _latest_process_record(log_dirs)
    if record is None:
        return {
            "available": False,
            "reason": "native process probe has not written a sample",
        }
    age = _timestamp_age_seconds(record.get("timestamp"))
    return {
        "available": True,
        "timestamp": record.get("timestamp"),
        "age_seconds": None if age is None else round(age, 3),
        "stale": age is None or age > FRESH_PROCESS_SAMPLE_SECONDS,
        "totals": _safe_totals(record.get("totals")),
        "by_role_mb": _footprint_by_role(record.get("processes")),
        "kernel_memory_pressure_level": _kernel_pressure_level(record.get("machine")),
    }


@router.get("/pressure")
def machine_pressure() -> dict[str, object]:
    """What the machine is under right now, cheap enough to poll.

    THE conditions half of a trustworthy timing row. The browser stamps this
    onto every deck-load row (see the frontend's machine-pressure.ts) so a
    latency number carries the machine state it was measured under instead of
    leaving a later reader to guess, which is how the register ended up with a
    waveform decode recorded at both 0.73 s and 7.53 s for the same work.

    Read-only, and deliberately NOT a process walk: this is sysctl, getloadavg
    and vm_stat, never the `ps` table that `/processes` reads out of the
    probe's log. It is served from a short shared cache and every response
    states the age of the sample it is handing back, so a caller can tell a
    fresh reading from a five-second-old one rather than assuming.

    Agent-native: `curl $ENGINE/api/v1/performance/telemetry/pressure`.

    Fields, all optional and all absent rather than zero when unreadable:
    `load_avg_1m` (1-minute kernel load average), `mem_free_mb` (free physical
    memory, `vm_stat` Pages free only, not the wider reclaimable figure),
    `swap_used_mb` (swap in use), `cache_age_ms` (age of this sample).
    `available` is false, with a `reason`, when nothing could be measured --
    notably inside the packaged app, whose payload stages `apps` and not
    `scripts`.
    """

    return read_machine_pressure()
