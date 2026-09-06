"""Bounded JSONL sample storage and the trend summary computed back off it.

Writing is capped rather than rotated-and-forgotten: a probe that silently
fills a disk is worse than one that stops appending. Reading is deliberately
separate from sampling, so a summary can be taken from a machine where the
probe is not currently running.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TREND_MINUTES = 30.0
TREND_AMBER_SLOPE_MB_PER_HOUR = 25.0
TREND_RED_SLOPE_MB_PER_HOUR = 100.0
UNLOAD_TOLERANCE_MB = 256.0


def append_bounded_jsonl(
    output_dir: Path, record: dict[str, Any], cap_bytes: int
) -> tuple[Path, bool]:
    output_dir.mkdir(parents=True, exist_ok=True)
    # UTC names and UTC record timestamps keep one cross-machine timeline.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # noqa: UP017
    path = output_dir / f"opendj-performance-{stamp}.jsonl"
    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        current_size = 0
    if current_size + len(payload) > cap_bytes:
        return path, False
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return path, True


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def _series_summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "first": round(values[0], 2),
        "last": round(values[-1], 2),
        "delta": round(values[-1] - values[0], 2),
        "min": round(min(values), 2),
        "median": round(statistics.median(values), 2),
        "p95": _percentile(values, 0.95) or 0.0,
        "max": round(max(values), 2),
    }


def _load_timed_records(
    output_dir: Path, cutoff: float
) -> tuple[list[tuple[datetime, dict[str, Any]]], int]:
    """Read every in-window record, counting the lines that would not parse."""

    rows: list[tuple[datetime, dict[str, Any]]] = []
    malformed_lines = 0
    for path in sorted(output_dir.glob("opendj-performance-*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (UnicodeError, ValueError):
                    malformed_lines += 1
                    continue
                if not isinstance(record, dict):
                    continue
                timestamp = _record_timestamp(record)
                if timestamp is None or timestamp.timestamp() < cutoff:
                    continue
                rows.append((timestamp, record))
    rows.sort(key=lambda item: item[0])
    return rows, malformed_lines


@dataclass
class _SampleAggregate:
    """Everything the trend summary accumulates over the sample window."""

    footprints: list[float]
    cpu_values: list[float]
    elapsed_seconds: list[float]
    role_values: dict[str, list[float]]
    process_families: set[tuple[tuple[str, int], ...]]
    max_active_jobs: int
    max_suspected_orphans: int
    deep_samples: int
    latest_deep: dict[str, Any] | None
    builds: dict[str, dict[str, Any]]


def _role_footprints(record: dict[str, Any]) -> tuple[list[tuple[str, int]], dict[str, float]]:
    """Split one sample's process list into its family shape and per-role MB."""

    family: list[tuple[str, int]] = []
    per_role: dict[str, float] = {}
    processes = record.get("processes")
    if not isinstance(processes, list):
        return family, per_role
    for process in processes:
        if not isinstance(process, dict):
            continue
        role = process.get("role")
        if not isinstance(role, str):
            continue
        pid = process.get("pid")
        if isinstance(pid, int):
            family.append((role, pid))
        value = process.get("physical_footprint_mb")
        if isinstance(value, (int, float)):
            per_role[role] = per_role.get(role, 0.0) + float(value)
    return family, per_role


def _active_engine_job_count(record: dict[str, Any]) -> int:
    engine = record.get("engine")
    jobs = engine.get("jobs") if isinstance(engine, dict) else None
    active = jobs.get("active") if isinstance(jobs, dict) else None
    return len(active) if isinstance(active, list) else 0


def _aggregate_samples(
    samples: list[tuple[datetime, dict[str, Any]]],
) -> _SampleAggregate:
    aggregate = _SampleAggregate(
        footprints=[],
        cpu_values=[],
        elapsed_seconds=[],
        role_values={},
        process_families=set(),
        max_active_jobs=0,
        max_suspected_orphans=0,
        deep_samples=0,
        latest_deep=None,
        builds={},
    )
    first_time = samples[0][0]
    for timestamp, record in samples:
        totals = record.get("totals")
        if not isinstance(totals, dict):
            continue
        footprint = totals.get("physical_footprint_mb")
        if isinstance(footprint, (int, float)):
            aggregate.footprints.append(float(footprint))
            aggregate.elapsed_seconds.append((timestamp - first_time).total_seconds())
        cpu = totals.get("cpu_percent")
        if isinstance(cpu, (int, float)):
            aggregate.cpu_values.append(float(cpu))
        family, per_role = _role_footprints(record)
        aggregate.process_families.add(tuple(sorted(family)))
        for role, value in per_role.items():
            aggregate.role_values.setdefault(role, []).append(value)
        aggregate.max_active_jobs = max(aggregate.max_active_jobs, _active_engine_job_count(record))
        orphan_count = record.get("suspected_orphan_count")
        if isinstance(orphan_count, int):
            aggregate.max_suspected_orphans = max(aggregate.max_suspected_orphans, orphan_count)
        deep = record.get("deep_vmmap")
        if isinstance(deep, dict):
            aggregate.deep_samples += 1
            aggregate.latest_deep = deep
        build = record.get("build")
        if isinstance(build, dict) and build.get("available") is True:
            key = str(build.get("git_sha_full") or build.get("built_at_utc") or build)
            aggregate.builds[key] = build
    return aggregate


def _linear_slope_mb_per_hour(
    elapsed_seconds: list[float], footprints: list[float]
) -> float | None:
    """Least-squares growth rate: the leak signal the point-in-time MB cannot show."""

    if len(footprints) < 2 or len(elapsed_seconds) != len(footprints):
        return None
    x_mean = statistics.mean(elapsed_seconds)
    y_mean = statistics.mean(footprints)
    denominator = sum((value - x_mean) ** 2 for value in elapsed_seconds)
    if denominator <= 0:
        return None
    # zip(strict=) is 3.10+; this package runs on /usr/bin/python3. The length
    # equality is asserted by the guard above instead.
    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(elapsed_seconds, footprints)  # noqa: B905
    )
    return round(numerator / denominator * 3600, 2)


def _swap_delta(samples: list[tuple[datetime, dict[str, Any]]]) -> dict[str, float | None]:
    """Machine-wide swap, so app growth is never confused with host saturation."""

    def _used(record: dict[str, Any]) -> float | None:
        machine = record.get("machine")
        value = machine.get("swap_used_mb") if isinstance(machine, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    first = _used(samples[0][1])
    last = _used(samples[-1][1])
    return {
        "first": first,
        "last": last,
        "delta": None if first is None or last is None else round(last - first, 2),
    }


def _window_summary(
    samples: list[tuple[datetime, dict[str, Any]]],
    total_record_count: int,
    malformed_lines: int,
) -> dict[str, Any]:
    return {
        "first": samples[0][0].isoformat().replace("+00:00", "Z"),
        "last": samples[-1][0].isoformat().replace("+00:00", "Z"),
        "duration_minutes": round((samples[-1][0] - samples[0][0]).total_seconds() / 60, 2),
        "sample_count": len(samples),
        "non_sample_record_count": total_record_count - len(samples),
        "malformed_lines": malformed_lines,
    }


def summarize_logs(output_dir: Path, hours: float) -> dict[str, Any]:
    """Summarize bounded samples into trend evidence without exposing commands."""

    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600  # noqa: UP017
    rows, malformed_lines = _load_timed_records(output_dir, cutoff)
    samples = [(timestamp, record) for timestamp, record in rows if record.get("kind") == "sample"]
    if not samples:
        return {
            "schema_version": 1,
            "available": False,
            "hours_requested": hours,
            "reason": "no process samples in requested window",
            "malformed_lines": malformed_lines,
        }

    aggregate = _aggregate_samples(samples)
    cpu_values = aggregate.cpu_values
    return {
        "schema_version": 1,
        "available": True,
        "hours_requested": hours,
        "window": _window_summary(samples, len(rows), malformed_lines),
        "physical_footprint_mb": _series_summary(aggregate.footprints),
        "linear_slope_mb_per_hour": _linear_slope_mb_per_hour(
            aggregate.elapsed_seconds, aggregate.footprints
        ),
        "cpu_percent": {
            "mean": round(statistics.mean(cpu_values), 2) if cpu_values else None,
            "p95": _percentile(cpu_values, 0.95),
            "max": round(max(cpu_values), 2) if cpu_values else None,
        },
        "by_role_mb": {
            role: _series_summary(values) for role, values in sorted(aggregate.role_values.items())
        },
        "unique_process_families": len(aggregate.process_families),
        "max_active_engine_jobs": aggregate.max_active_jobs,
        "max_suspected_orphans": aggregate.max_suspected_orphans,
        "swap_used_mb": _swap_delta(samples),
        "deep_vmmap_sample_count": aggregate.deep_samples,
        "latest_deep_vmmap": aggregate.latest_deep,
        "builds": list(aggregate.builds.values()),
    }


def _parse_since(value: str) -> datetime:
    """Parse one explicit ISO 8601 boundary, refusing ambiguous local time."""

    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--since must be ISO 8601, got {value!r}") from exc
    if result.tzinfo is None:
        raise ValueError("--since must include a timezone, for example 2026-09-02T12:00:00Z")
    return result.astimezone(timezone.utc)  # noqa: UP017


def _process_series(
    samples: list[tuple[datetime, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Return PID-stable footprint slopes without copying command strings."""

    by_pid: dict[str, tuple[str, list[float], list[float]]] = {}
    first_time = samples[0][0]
    for timestamp, record in samples:
        processes = record.get("processes")
        if not isinstance(processes, list):
            continue
        elapsed = (timestamp - first_time).total_seconds()
        for process in processes:
            if not isinstance(process, dict) or not isinstance(process.get("pid"), int):
                continue
            value = process.get("physical_footprint_mb")
            if not isinstance(value, (int, float)):
                continue
            key = str(process["pid"])
            role = str(process.get("role", "unknown"))
            prior = by_pid.get(key)
            if prior is None:
                by_pid[key] = (role, [elapsed], [float(value)])
            else:
                prior[1].append(elapsed)
                prior[2].append(float(value))
    return {
        pid: {
            "role": role,
            "sample_count": len(values),
            "physical_footprint_mb": _series_summary(values),
            "slope_mb_per_hour": _linear_slope_mb_per_hour(elapsed, values),
        }
        for pid, (role, elapsed, values) in sorted(by_pid.items(), key=lambda item: int(item[0]))
    }


def _loaded_deck_count(record: dict[str, Any]) -> int | None:
    ring = record.get("browser_perf_ring")
    value = ring.get("loaded_deck_count") if isinstance(ring, dict) else None
    return value if isinstance(value, int) and 0 <= value <= 4 else None


def _deck_normalized_footprints(
    samples: list[tuple[datetime, dict[str, Any]]],
) -> dict[str, Any]:
    """Per-deck footprints, with the zero-deck samples EXCLUDED, not divided.

    Dividing by `max(1, decks)` treated a measured zero-deck sample as a
    one-deck sample, so the whole raw process footprint entered the series
    labelled as a per-deck value. That is a fabricated denominator, and it
    drags the series toward a number no deck ever cost. A zero-deck sample has
    no per-deck value to report, so it is omitted and COUNTED: the count is
    what stops the omission from being silent, and it distinguishes an empty
    series caused by no measurable ring from one caused by an idle app.
    """

    values: list[float] = []
    excluded_zero_deck = 0
    for _, record in samples:
        decks = _loaded_deck_count(record)
        if decks is None:
            continue
        totals = record.get("totals")
        if not isinstance(totals, dict) or not isinstance(
            totals.get("physical_footprint_mb"), (int, float)
        ):
            continue
        if decks == 0:
            excluded_zero_deck += 1
            continue
        values.append(round(float(totals["physical_footprint_mb"]) / decks, 2))
    return {"values": values, "excluded_zero_deck": excluded_zero_deck}


def _unload_check(
    samples: list[tuple[datetime, dict[str, Any]]],
    history: list[tuple[datetime, dict[str, Any]]],
) -> dict[str, Any]:
    """Compare a windowed load cycle to the first post-boot sample for its shell."""

    def footprint_rows(
        records: list[tuple[datetime, dict[str, Any]]],
    ) -> list[tuple[dict[str, Any], int | None]]:
        return [
            (record, _loaded_deck_count(record))
            for _, record in records
            if isinstance(record.get("totals"), dict)
            and isinstance(record["totals"].get("physical_footprint_mb"), (int, float))
        ]

    post_boot_baselines: dict[int, float | None] = {}
    for record, decks in footprint_rows(history):
        shell_pid = record.get("shell_pid")
        if not isinstance(shell_pid, int) or shell_pid in post_boot_baselines:
            continue
        footprint = float(record["totals"]["physical_footprint_mb"])
        post_boot_baselines[shell_pid] = footprint if decks == 0 else None

    four_loaded_seen: set[int] = set()
    completed: tuple[float, float] | None = None
    for record, decks in footprint_rows(samples):
        shell_pid = record.get("shell_pid")
        if not isinstance(shell_pid, int):
            continue
        baseline = post_boot_baselines.get(shell_pid)
        if baseline is None:
            continue
        footprint = float(record["totals"]["physical_footprint_mb"])
        if decks == 4:
            four_loaded_seen.add(shell_pid)
        elif decks == 0 and shell_pid in four_loaded_seen:
            completed = (baseline, footprint)
    if completed is None:
        return {
            "available": False,
            "reason": "need post-boot baseline, four loaded decks, and later all-unloaded sample",
        }
    baseline, latest_unloaded = completed
    delta = round(latest_unloaded - baseline, 2)
    return {
        "available": True,
        "baseline_mb": baseline,
        "after_unload_mb": latest_unloaded,
        "delta_mb": delta,
        "tolerance_mb": UNLOAD_TOLERANCE_MB,
        "passed": delta <= UNLOAD_TOLERANCE_MB,
    }


def trend_logs(output_dir: Path, since: str) -> dict[str, Any]:
    """Assess resource growth from existing JSONL only, with a process verdict."""

    boundary = _parse_since(since)
    rows, malformed_lines = _load_timed_records(output_dir, boundary.timestamp())
    if malformed_lines:
        return {
            "available": False,
            "verdict": "AMBER",
            "exit_code": 2,
            "reason": (
                f"refusing trend verdict: {malformed_lines} malformed JSONL "
                "record(s) in the selected log window"
            ),
        }
    samples = [(timestamp, record) for timestamp, record in rows if record.get("kind") == "sample"]
    if not samples:
        return {
            "available": False,
            "verdict": "AMBER",
            "exit_code": 0,
            "reason": "no process samples since boundary",
        }
    duration_minutes = (samples[-1][0] - samples[0][0]).total_seconds() / 60
    processes = _process_series(samples)
    slopes = [
        item["slope_mb_per_hour"]
        for item in processes.values()
        if isinstance(item["slope_mb_per_hour"], float)
    ]
    max_slope = max(slopes, default=None)
    orphan_count = max(
        (
            int(record.get("suspected_orphan_count", 0))
            for _, record in samples
            if isinstance(record.get("suspected_orphan_count", 0), int)
        ),
        default=0,
    )
    engine_rss = [
        float(process["resident_mb"])
        for _, record in samples
        for process in record.get("processes", [])
        if isinstance(process, dict)
        and process.get("role") == "python-engine"
        and isinstance(process.get("resident_mb"), (int, float))
    ]
    history, _ = _load_timed_records(output_dir, float("-inf"))
    history_samples = [
        (timestamp, record) for timestamp, record in history if record.get("kind") == "sample"
    ]
    unload = _unload_check(samples, history_samples)
    normalized = _deck_normalized_footprints(samples)
    reasons: list[str] = []
    # The sufficiency floor gates only the SLOPE-derived signal. A window
    # shorter than TREND_MINUTES has not observed enough time to extrapolate a
    # rate, so a steep slope in that window is an observation, not a finding
    # (verification.md: a tool that cannot measure reports UNKNOWN, never a
    # verdict). Orphan count and a failed unload check are direct observations,
    # not extrapolations: an orphan that exists, exists, and a failed unload
    # check failed, regardless of how long the window was. Suppressing those
    # behind the floor would report UNKNOWN with exit 0 for a real, directly
    # observed failure, which is worse than the false alarm the floor exists
    # to prevent (issue #1404 review, PR #1407).
    sufficient_window = duration_minutes >= TREND_MINUTES
    slope_exceeds_red = max_slope is not None and max_slope > TREND_RED_SLOPE_MB_PER_HOUR
    red_from_observation = orphan_count > 0 or unload.get("passed") is False
    if orphan_count:
        reasons.append(f"{orphan_count} suspected orphan(s)")
    if unload.get("passed") is False:
        reasons.append(f"unload retained {unload['delta_mb']} MB above baseline")
    if red_from_observation:
        verdict = "RED"
        if slope_exceeds_red:
            if sufficient_window:
                reasons.append(f"process slope {max_slope} MB/hour")
            else:
                reasons.append(
                    f"process slope {max_slope} MB/hour observed but not counted "
                    f"(only {duration_minutes:.1f} minutes sampled, need {TREND_MINUTES:.0f})"
                )
    elif not sufficient_window:
        verdict = "UNKNOWN"
        reasons.append(f"only {duration_minutes:.1f} minutes sampled, need {TREND_MINUTES:.0f}")
    elif slope_exceeds_red:
        verdict = "RED"
        reasons.append(f"process slope {max_slope} MB/hour")
    elif max_slope is None:
        verdict = "AMBER"
        reasons.append("no process slope is measurable")
    elif max_slope > TREND_AMBER_SLOPE_MB_PER_HOUR:
        verdict = "AMBER"
        reasons.append(f"process slope {max_slope} MB/hour")
    else:
        verdict = "GREEN"
        reasons.append("no growth threshold crossed")
    return {
        "available": True,
        "since": boundary.isoformat().replace("+00:00", "Z"),
        "window": _window_summary(samples, len(rows), malformed_lines),
        "per_process": processes,
        "deck_load_normalized_footprint_mb": normalized["values"],
        "deck_load_normalized_excluded_zero_deck_samples": normalized["excluded_zero_deck"],
        "orphan_count": orphan_count,
        "engine_rss_mb": _series_summary(engine_rss),
        "engine_port": next(
            (
                engine["port"]
                for _, record in reversed(samples)
                if isinstance(record.get("engine"), dict)
                and isinstance((engine := record["engine"]).get("port"), int)
            ),
            None,
        ),
        "unload_check": unload,
        "verdict": verdict,
        "verdict_reason": "; ".join(reasons),
        "exit_code": 1 if verdict == "RED" else 0,
    }
