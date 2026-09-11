"""Ten-minute live preview engine health probe for perf KPI (issue #1506)."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_UNHEALTHY = 1
EXIT_RESTARTED = 2


@dataclass(frozen=True)
class HealthConfig:
    health_url: str
    state_file: Path
    health_log: Path
    timeout_s: float
    failure_threshold: int
    restart_command: tuple[str, ...]
    engine_log_path: Path


ProbeFn = Callable[[str, float], str | None]
RestartFn = Callable[[tuple[str, ...]], tuple[bool, str]]
TailFn = Callable[[Path, int], list[str]]


def _load_state(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"consecutive_failures": 0}
    raw = json.loads(path.read_text(encoding="utf-8"))
    failures = raw.get("consecutive_failures")
    if not isinstance(failures, int) or failures < 0:
        raise ValueError(f"invalid health state: {path}")
    return {"consecutive_failures": failures}


def _write_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def default_probe(url: str, timeout_s: float) -> str | None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return str(error)
    if 200 <= status < 300:
        return None
    return f"HTTP {status}"


def default_restart(command: tuple[str, ...]) -> tuple[bool, str]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    detail = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode == 0:
        return True, detail or "restart completed"
    return False, f"exit {completed.returncode}: {detail}"


def default_tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]


def run_health_tick(
    config: HealthConfig,
    *,
    probe: ProbeFn = default_probe,
    restart: RestartFn = default_restart,
    tail_log: TailFn = default_tail,
) -> int:
    state = _load_state(config.state_file)
    error = probe(config.health_url, config.timeout_s)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if error is None:
        state["consecutive_failures"] = 0
        _write_state(config.state_file, state)
        _append_jsonl(
            config.health_log,
            {"ts": stamp, "event": "healthy", "health_url": config.health_url},
        )
        return 0

    failures = state["consecutive_failures"] + 1
    state["consecutive_failures"] = failures
    _write_state(config.state_file, state)
    _append_jsonl(
        config.health_log,
        {
            "ts": stamp,
            "event": "unhealthy",
            "health_url": config.health_url,
            "error": error,
            "consecutive_failures": failures,
        },
    )
    if failures < config.failure_threshold:
        return EXIT_UNHEALTHY

    restarted, detail = restart(config.restart_command)
    _append_jsonl(
        config.health_log,
        {
            "ts": stamp,
            "event": "restart",
            "detail": detail,
            "succeeded": restarted,
            "consecutive_failures": failures,
            "engine_log_tail": tail_log(config.engine_log_path, 20),
        },
    )
    state["consecutive_failures"] = 0
    _write_state(config.state_file, state)
    return EXIT_RESTARTED if restarted else EXIT_UNHEALTHY


def build_restart_command(engine_label: str) -> tuple[str, ...]:
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    return (
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{uid}/{engine_label}",
    )


def parse_restart_command(raw: str) -> tuple[str, ...]:
    parts = tuple(shlex.split(raw))
    if not parts:
        raise ValueError("restart command must not be empty")
    return parts
