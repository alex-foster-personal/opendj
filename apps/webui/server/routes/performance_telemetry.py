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
    ShellRegistration,
    live_process_family,
    opendj_process_name,
    read_machine_pressure,
    read_shell_registration,
    valid_kernel_pressure_level,
)

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path
from ..shell_output_health import data_dir_from_state_db
from ..state_paths import resolve_state_db_path

router = APIRouter(prefix="/performance/telemetry", tags=["performance-telemetry"])
MOUNT_UNDER_API_PREFIX = True
log = logging.getLogger(__name__)

DEFAULT_PROCESS_LOG_DIRS = (
    Path.home() / "Library/Application Support/OpenDJ Diagnostics/performance",
    Path.home() / ".local/share/music-dj-tools/performance",
)
MAX_PROCESS_RECORD_BYTES = 512 * 1024
FRESH_PROCESS_SAMPLE_SECONDS = 45.0
# How far in the FUTURE a probe timestamp may sit and still count as fresh:
# the probe and the engine read the same wall clock, so only rounding and a
# write in flight can put a sample ahead of now. Beyond this the clock moved
# back after the write (or the record is wrong), so its age is unknown and it
# is treated as stale, never as fresh.
FUTURE_PROCESS_SAMPLE_SKEW_SECONDS = 5.0
SAFE_TOTAL_KEYS = frozenset(
    {
        "physical_footprint_mb",
        "summed_lifetime_peak_mb",
        "cpu_percent",
        "process_count",
        "rss_mb",
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


class FrameProbeSample(BaseModel):
    """One window of the opt-in in-page frame probe (PERFPROBE-01).

    Measured inside the page so every shell, including WKWebView with no
    debugging port, reports the same numbers the same way.
    """

    shell: Literal["electron", "tauri", "browser"]
    window_ms: float = Field(ge=0)
    frames: int = Field(ge=0)
    frames_sampled: int = Field(ge=0)
    fps: float = Field(ge=0)
    p50_ms: float | None = Field(default=None, ge=0)
    p95_ms: float | None = Field(default=None, ge=0)
    p99_ms: float | None = Field(default=None, ge=0)
    max_ms: float | None = Field(default=None, ge=0)
    long_frames: int = Field(ge=0)
    xruns: int | None = Field(default=None, ge=0)


class LiveHandlesSample(BaseModel):
    """Live in-page handle totals from the opt-in probe (PERF-LEAK-11789)."""

    intervals: int = Field(ge=0)
    long_timeouts: int = Field(ge=0)
    window_listeners: int = Field(ge=0)
    rafs: int = Field(ge=0)


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
    # Null unless the page was opened with ?perfprobe=1 (PERFPROBE-01).
    frame_probe: FrameProbeSample | None = None
    # Null unless ?perfprobe=1 armed the live handles probe (PERF-LEAK-11789).
    live_handles: LiveHandlesSample | None = None


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
        "pressure": read_machine_pressure(),
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
    # Signed: a negative age is a future-dated sample, judged by the caller.
    return (datetime.now(UTC) - parsed).total_seconds()


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


def _member_memory_mb(member: dict[str, object]) -> float | None:
    footprint = member.get("physical_footprint_mb")
    if isinstance(footprint, (int, float)):
        return float(footprint)
    rss = member.get("rss_mb")
    if isinstance(rss, (int, float)):
        return float(rss)
    return None


def _footprint_by_role_members(members: list[dict[str, object]]) -> dict[str, float]:
    by_role: dict[str, float] = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        role = member.get("role")
        if not isinstance(role, str):
            continue
        mb = _member_memory_mb(member)
        if mb is None:
            continue
        by_role[role] = round(by_role.get(role, 0.0) + mb, 1)
    return by_role


def _safe_totals(totals: object) -> dict[str, float]:
    if not isinstance(totals, dict):
        return {}
    return {
        key: value
        for key, value in totals.items()
        if key in SAFE_TOTAL_KEYS and isinstance(value, (int, float))
    }


def _family_totals_and_by_role(
    members: list[dict[str, object]],
    record: dict[str, object] | None,
    *,
    stale: bool,
    live_pids: set[int],
    jsonl_processes: object,
    probe_fresh: bool,
) -> tuple[dict[str, float], dict[str, float]]:
    """Totals and by_role_mb: record when present; live sums when probe does not apply."""

    probe_applies = isinstance(jsonl_processes, list) and _probe_applies(
        live_pids, jsonl_processes, probe_fresh
    )
    if record is not None:
        totals = _safe_totals(record.get("totals"))
        by_role = (
            _footprint_by_role_members(members)
            if probe_applies and not stale
            else _footprint_by_role(jsonl_processes)
        )
        totals["process_count"] = float(len(members))
        if probe_applies and not stale:
            live_sum = 0.0
            has_any = False
            for member in members:
                mb = _member_memory_mb(member)
                if mb is not None:
                    live_sum += mb
                    has_any = True
            if has_any:
                totals["physical_footprint_mb"] = round(live_sum, 1)
        return totals, by_role
    rss_sum = 0.0
    has_rss = False
    for member in members:
        rss = member.get("rss_mb")
        if isinstance(rss, (int, float)):
            rss_sum += float(rss)
            has_rss = True
    live_totals: dict[str, float] = {"process_count": float(len(members))}
    if has_rss:
        live_totals["rss_mb"] = round(rss_sum, 1)
    return live_totals, _footprint_by_role_members(members)


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


def _probe_record_is_this_app(live_pids: set[int], jsonl_processes: list[object]) -> bool:
    """True when the probe record names at least one pid of this engine's family."""

    return any(
        isinstance(process, dict) and process.get("pid") in live_pids
        for process in jsonl_processes
    )


# Probe-only fields a live member takes from the probe row for the same pid.
# The live walk measures resident MB itself; the probe adds what only it knows:
# the role (``webkit-webcontent`` tells the page its own process is measured)
# and the macOS physical footprint.
_PROBE_ONLY_FIELDS = ("role", "physical_footprint_mb")


def _live_rows(
    live_members: list[dict[str, Any]],
) -> tuple[list[dict[str, object]], dict[int, dict[str, object]]]:
    members: list[dict[str, object]] = []
    live_by_pid: dict[int, dict[str, object]] = {}
    for live in live_members:
        member: dict[str, object] = {
            key: value for key, value in live.items() if key != "pid"
        }
        member["source"] = MEMBER_SOURCE_LIVE
        members.append(member)
        pid = live.get("pid")
        if isinstance(pid, int):
            live_by_pid[pid] = member
    return members, live_by_pid


def _probe_applies(
    live_pids: set[int], jsonl_processes: list[object], probe_fresh: bool
) -> bool:
    # A stale probe sample describes processes as they were, not as they are:
    # none of its rows or fields enter the current figure.
    # The native probe watches one installed app. When none of the pids it
    # recorded is in this engine's own family (or there is no family to check
    # against: ownership is never assumed), it measured another Open DJ (the
    # installed app beside a dev checkout, say), and its members are not this
    # app's memory.
    return probe_fresh and _probe_record_is_this_app(live_pids, jsonl_processes)


def _adopt_probe_fields(target: dict[str, object], process: dict[str, Any]) -> None:
    probe = _jsonl_member(process)
    taken = [key for key in _PROBE_ONLY_FIELDS if key in probe]
    for key in taken:
        target[key] = probe[key]
    # Names what came from the probe, so a client that keeps this body past the
    # probe sample's own lifetime can drop exactly those fields and keep the
    # live reading.
    target["probe_fields"] = taken


def _merge_members(
    live_members: list[dict[str, Any]],
    live_pids: set[int],
    jsonl_processes: object,
    *,
    probe_fresh: bool,
) -> list[dict[str, object]]:
    members, live_by_pid = _live_rows(live_members)
    if not isinstance(jsonl_processes, list) or not _probe_applies(
        live_pids, jsonl_processes, probe_fresh
    ):
        return members
    for process in jsonl_processes:
        if not isinstance(process, dict):
            continue
        pid = process.get("pid")
        if isinstance(pid, int) and pid in live_pids:
            target = live_by_pid.get(pid)
            if target is not None:
                _adopt_probe_fields(target, process)
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
    # The walk's root is this engine's own pid unless app.state names another
    # (``process_family_root_pid``, set like ``performance_process_log_dirs``
    # after construction): the seam a test uses to root the walk at a real
    # process of its own, including one that has exited.
    root_pid = getattr(request.app.state, "process_family_root_pid", None)
    registration = _shell_registration(request)
    family = live_process_family(engine_pid=root_pid, registered_shell_pid=registration.pid)
    body = _process_body(
        family.members,
        family.pids,
        record,
        read_machine_pressure(),
        family.webkit_association,
    )
    body["shell_registration"] = registration.report()
    return body


# The last rejection reason logged, so a 10 s poll logs each reason once.
_rejection_logged: dict[str, str | None] = {"reason": None}


def _shell_registration(request: Request) -> ShellRegistration:
    """The shell registered with this engine (``.engine.parent`` in its data dir).

    The directory is the engine's data dir, the one its shell writes to,
    unless ``app.state.shell_registration_dir`` names another (set after
    construction like ``performance_process_log_dirs``: the seam a test uses
    to register a real process of its own). A rejected registration is
    logged once per reason and reported in the body, never used.
    """

    state = request.app.state
    directory = (
        state.shell_registration_dir
        if hasattr(state, "shell_registration_dir")
        else data_dir_from_state_db(resolve_state_db_path(request))
    )
    registration = read_shell_registration(directory)
    reason = registration.reason if registration.status == "rejected" else None
    if reason is not None and reason != _rejection_logged["reason"]:
        log.warning("shell registration in %s ignored: %s", directory, reason)
    _rejection_logged["reason"] = reason
    return registration


def _sample_is_stale(record: dict[str, object] | None, age: float | None) -> bool:
    return record is not None and (
        age is None
        or age > FRESH_PROCESS_SAMPLE_SECONDS
        or age < -FUTURE_PROCESS_SAMPLE_SKEW_SECONDS
    )


def _process_body(
    live_members: list[dict[str, Any]],
    live_pids: set[int],
    record: dict[str, object] | None,
    pressure: dict[str, object],
    webkit_association: dict[str, str],
) -> dict[str, object]:
    """The /processes body from one live walk, the latest probe record and pressure.

    With no live member the body is ``available: false``, whatever the probe
    wrote: the live family is the only thing that ties a probe record to THIS
    engine (an overlapping pid), and freshness alone does not, so a record
    read without it could be the installed app's beside a dev engine, and an
    empty family would read as a measured zero. The body still carries
    ``stale`` and ``age_seconds`` when there is a record: the frontend reads
    them from an unavailable body too, so the hover can say a stale sample's
    footprints were left out.
    """

    age = None if record is None else _timestamp_age_seconds(record.get("timestamp"))
    stale = _sample_is_stale(record, age)
    probe_fresh = record is not None and not stale
    if not live_members:
        if record is None:
            reason = "no live opendj-* processes and native process probe has not written a sample"
        elif stale:
            reason = "no live opendj-* processes and the native process probe sample is stale"
        else:
            reason = (
                "no live opendj-* processes, so the native process probe sample cannot be "
                "attributed to this engine"
            )
        unavailable: dict[str, object] = {"available": False, "reason": reason}
        if record is not None:
            unavailable["stale"] = stale
            unavailable["age_seconds"] = None if age is None else round(age, 3)
        return unavailable

    members = _merge_members(
        live_members,
        live_pids,
        record.get("processes") if record else None,
        probe_fresh=probe_fresh,
    )
    totals, by_role = _family_totals_and_by_role(
        members,
        record,
        stale=stale,
        live_pids=live_pids,
        jsonl_processes=record.get("processes") if record else None,
        probe_fresh=probe_fresh,
    )

    body: dict[str, object] = {
        "available": True,
        "members": members,
        "totals": totals,
        "by_role_mb": by_role,
        "webkit_association": dict(webkit_association),
    }
    body.update(_pressure_overlay_fields(pressure))

    if record is not None:
        body["timestamp"] = record.get("timestamp")
        body["age_seconds"] = None if age is None else round(age, 3)
        body["stale"] = stale

    return body


@router.get("/pressure")
def machine_pressure() -> dict[str, object]:
    """What the machine is under right now, cheap enough to poll."""

    return read_machine_pressure()
