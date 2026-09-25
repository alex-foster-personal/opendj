"""Bounded client performance samples and latest native process footprint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from apps.shared.machine_pressure import (
    live_process_family_state,
    opendj_process_name,
    read_machine_pressure,
    valid_kernel_pressure_level,
)

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path

router = APIRouter(prefix="/performance/telemetry", tags=["performance-telemetry"])
log = logging.getLogger(__name__)

DEFAULT_PROCESS_LOG_DIRS = (
    Path.home() / "Library/Application Support/OpenDJ Diagnostics/performance",
    Path.home() / ".local/share/music-dj-tools/performance",
)
MAX_PROCESS_RECORD_BYTES = 512 * 1024
FRESH_PROCESS_SAMPLE_SECONDS = 45.0
SAFE_TOTAL_KEYS = frozenset(
    {
        "physical_footprint_mb",
        "summed_lifetime_peak_mb",
        "cpu_percent",
        "process_count",
    }
)
# Every merged member names where its footprint came from. A `live` member was
# walked just now (psutil, `rss_mb`); a `probe_log` member is a process from the
# native probe's last JSONL record that is not live now, and that record can be
# days old (`stale`). Consumers measuring the current family count `live` only.
MEMBER_SOURCE_LIVE = "live"
MEMBER_SOURCE_PROBE_LOG = "probe_log"


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


def _machine_pressure(request: Request) -> dict[str, object]:
    """The machine pressure reading, from ``app.state.machine_pressure_reader`` if set.

    Same seam as the log directories: a test on a host whose kernel pressure
    is readable can still exercise the unread case without patching.
    """
    reader = getattr(request.app.state, "machine_pressure_reader", read_machine_pressure)
    reading: dict[str, object] = reader()
    return reading


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
        "pressure": _machine_pressure(request),
    }
    log_dir = Path(
        getattr(request.app.state, "performance_log_dir", DEFAULT_LOG_DIR)
    )
    path = daily_log_path(log_dir, "webui-performance", time.gmtime())
    stored = append_json_record(path, record)
    if not stored:
        log.warning("performance sample %s not stored: daily cap reached at %s", event_id, path)
    request.app.state.last_client_performance_sample = {
        "event_id": event_id,
        **payload.model_dump(),
    }
    return ClientPerformanceSampleOut(event_id=event_id, stored=stored)


@router.get("/client-samples")
def latest_client_performance_sample(request: Request) -> dict[str, object]:
    """Return the last accepted sample for this process, or 404 when none."""
    sample = getattr(request.app.state, "last_client_performance_sample", None)
    if sample is None:
        raise HTTPException(status_code=404, detail="no client performance sample yet")
    return sample


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
    if not isinstance(totals, dict):
        return {}
    return {
        key: value
        for key, value in totals.items()
        if key in SAFE_TOTAL_KEYS and isinstance(value, (int, float))
    }


def _jsonl_member(process: dict[str, object]) -> dict[str, object]:
    command = process.get("command")
    name = opendj_process_name(command) if isinstance(command, str) else "unnamed"
    member: dict[str, object] = {"name": name, "source": MEMBER_SOURCE_PROBE_LOG}
    footprint = process.get("physical_footprint_mb")
    if isinstance(footprint, (int, float)):
        member["physical_footprint_mb"] = round(float(footprint), 1)
    role = process.get("role")
    if isinstance(role, str):
        member["role"] = role
    return member


def _merge_members(
    live_members: list[dict[str, Any]],
    live_pids: set[int],
    jsonl_processes: object,
) -> list[dict[str, object]]:
    members: list[dict[str, object]] = [
        {**member, "source": MEMBER_SOURCE_LIVE} for member in live_members
    ]
    if not isinstance(jsonl_processes, list):
        return members
    for process in jsonl_processes:
        if not isinstance(process, dict):
            continue
        pid = process.get("pid")
        if isinstance(pid, int) and pid in live_pids:
            continue
        members.append(_jsonl_member(process))
    return members


def _pressure_overlay_fields(pressure: dict[str, object]) -> dict[str, object]:
    overlay: dict[str, object] = {}
    kernel = valid_kernel_pressure_level(pressure.get("kernel_memory_pressure_level"))
    if kernel is not None:
        overlay["kernel_memory_pressure_level"] = kernel
    for key in (
        "churn_score",
        "band",
        "sample_interval_ms",
        "compressed_mb",
        "sample_wall_ms",
        "sample_wall_p95_ms",
    ):
        value = pressure.get(key)
        if value is not None:
            overlay[key] = value
    return overlay


@router.get("/processes")
def latest_process_telemetry(request: Request) -> dict[str, object]:
    """Return a privacy-reduced Activity Monitor-style app breakdown."""

    configured = getattr(
        request.app.state, "performance_process_log_dirs", DEFAULT_PROCESS_LOG_DIRS
    )
    log_dirs = tuple(Path(directory) for directory in configured)
    record = _latest_process_record(log_dirs)
    live_members, live_pids = live_process_family_state()
    pressure = _machine_pressure(request)

    if not live_members and record is None:
        return {
            "available": False,
            "reason": (
                "no live opendj-* processes and native process probe has not written a sample"
            ),
        }

    body: dict[str, object] = {
        "available": True,
        "members": _merge_members(
            live_members,
            live_pids,
            record.get("processes") if record else None,
        ),
    }
    body.update(_pressure_overlay_fields(pressure))

    if record is not None:
        age = _timestamp_age_seconds(record.get("timestamp"))
        body["timestamp"] = record.get("timestamp")
        body["age_seconds"] = None if age is None else round(age, 3)
        body["stale"] = age is None or age > FRESH_PROCESS_SAMPLE_SECONDS
        body["totals"] = _safe_totals(record.get("totals"))
        body["by_role_mb"] = _footprint_by_role(record.get("processes"))

    return body


@router.get("/pressure")
def machine_pressure(request: Request) -> dict[str, object]:
    """What the machine is under right now, cheap enough to poll."""

    return _machine_pressure(request)
