"""Validate Chrome Task Manager capture artifacts (opendj.chrome-task-manager.v1).

Stdlib-only, 3.9-compatible. Run as:
    python3 scripts/perf_health/chrome_task_manager.py capture.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

SCHEMA_ID = "opendj.chrome-task-manager.v1"
REQUIRED_PROCESSES = ("renderer", "gpu", "browser")
REQUIRED_PROCESS_FIELDS = ("task", "cpu_percent", "memory_mb")


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def validate_capture(data: dict) -> list[str]:
    """Return error strings; empty list means valid."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["capture must be a JSON object"]

    schema = data.get("schema")
    if schema != SCHEMA_ID:
        errors.append(f"schema must be {SCHEMA_ID!r}, got {schema!r}")

    unavailable = data.get("unavailable_reason")
    if unavailable is not None:
        if not isinstance(unavailable, str) or not unavailable.strip():
            errors.append("unavailable_reason must be a non-empty string when present")
        return errors

    processes = data.get("processes")
    if not isinstance(processes, dict):
        errors.append("processes must be an object when unavailable_reason is absent")
        return errors

    for key in REQUIRED_PROCESSES:
        if key not in processes:
            errors.append(f"missing required process key: {key}")
            continue

        proc = processes[key]
        if not isinstance(proc, dict):
            errors.append(f"processes.{key} must be an object")
            continue

        task = proc.get("task")
        if not isinstance(task, str) or not task.strip():
            errors.append(f"processes.{key}.task must be a non-empty string")

        for field in ("cpu_percent", "memory_mb"):
            value = proc.get(field)
            if not _is_finite_number(value):
                errors.append(
                    f"processes.{key}.{field} must be a finite number, got {value!r}"
                )

    return errors


def parse_capture(path: str | Path) -> tuple[dict, list[str]]:
    """Read JSON from path and validate. Returns (data, errors)."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f"cannot read {path}: {exc}"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, [f"invalid JSON in {path}: {exc}"]

    if not isinstance(data, dict):
        return {}, ["capture must be a JSON object"]

    return data, validate_capture(data)


def breakdown_lines(data: dict) -> list[str]:
    """Human-readable breakdown lines for a valid capture."""
    unavailable = data.get("unavailable_reason")
    if isinstance(unavailable, str) and unavailable.strip():
        return [f"unavailable: {unavailable.strip()}"]

    processes = data.get("processes", {})
    if not isinstance(processes, dict):
        return ["unavailable: processes missing or invalid"]

    lines: list[str] = []
    labels = {
        "renderer": "renderer (tab)",
        "gpu": "GPU Process",
        "browser": "Browser",
    }
    for key in REQUIRED_PROCESSES:
        proc = processes.get(key, {})
        if not isinstance(proc, dict):
            continue
        cpu = proc.get("cpu_percent")
        mem = proc.get("memory_mb")
        label = labels.get(key, key)
        lines.append(f"{label}: cpu={cpu}% memory={mem} MB")

    return lines


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: chrome_task_manager.py <capture.json>", file=sys.stderr)
        return 1

    _data, errors = parse_capture(args[0])
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    for line in breakdown_lines(_data):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
