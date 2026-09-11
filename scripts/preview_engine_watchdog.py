"""Probe a preview engine over HTTP and restart it only after bounded failure.

Requirements:
- [if] GET ``--health-url`` returns a 2xx response [then] persisted failures reset
  to zero and stdout records ``healthy`` [else the watchdog is broken].
- [if] one probe fails below ``--failure-threshold`` [then] it records the failure
  and does not restart [else the watchdog is broken].
- [if] consecutive failed probes reach the threshold [then] it runs the explicit
  restart command at most once per cooldown [else the watchdog is broken].

This is stdlib-only because the Air's launchd agent must work without a checkout
venv. It measures a real HTTP request, rather than a PID or TCP connection, so a
listening engine whose event loop is wedged is unhealthy. The restart command is
required deliberately: the watchdog must never guess which preview it controls.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

EXIT_UNHEALTHY = 1
EXIT_RESTARTED = 2
EXIT_RESTART_FAILED = 3


@dataclass(frozen=True)
class WatchdogConfig:
    health_url: str
    state_file: Path
    restart_command: tuple[str, ...]
    timeout_s: float
    failure_threshold: int
    restart_cooldown_s: float
    restart_timeout_s: float


# ----- State ------------------------------------------------------------------------


def _load_state(path: Path) -> dict[str, int | float]:
    if not path.exists():
        return {"consecutive_failures": 0}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"watchdog state must be an object: {path}")
    failures = raw.get("consecutive_failures")
    if not isinstance(failures, int) or failures < 0:
        raise ValueError(f"watchdog state has invalid consecutive_failures: {path}")
    state: dict[str, int | float] = {"consecutive_failures": failures}
    last_restart = raw.get("last_restart_epoch")
    if last_restart is not None:
        if not isinstance(last_restart, (int, float)) or last_restart < 0:
            raise ValueError(f"watchdog state has invalid last_restart_epoch: {path}")
        state["last_restart_epoch"] = last_restart
    return state


def _write_state(path: Path, state: dict[str, int | float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


# ----- Probe and recovery -----------------------------------------------------------


def _probe_health(url: str, timeout_s: float) -> str | None:
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


def _restart(command: tuple[str, ...], timeout_s: float) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"restart command timed out after {timeout_s}s"
    if completed.returncode == 0:
        return True, "restart command completed"
    detail = completed.stderr.strip() or completed.stdout.strip()
    return False, f"restart command exited {completed.returncode}: {detail}"


def run(config: WatchdogConfig, *, now: float | None = None) -> int:
    """Run one probe tick and return an explicit operational exit status."""
    state = _load_state(config.state_file)
    error = _probe_health(config.health_url, config.timeout_s)
    if error is None:
        state["consecutive_failures"] = 0
        _write_state(config.state_file, state)
        _emit("healthy", health_url=config.health_url)
        return 0

    failures = int(state["consecutive_failures"]) + 1
    state["consecutive_failures"] = failures
    _write_state(config.state_file, state)
    _emit("unhealthy", error=error, failures=failures, health_url=config.health_url)
    if failures < config.failure_threshold:
        return EXIT_UNHEALTHY

    current_time = time.time() if now is None else now
    last_restart = state.get("last_restart_epoch")
    if isinstance(last_restart, (int, float)) and (
        current_time - last_restart < config.restart_cooldown_s
    ):
        _emit(
            "restart_suppressed",
            cooldown_s=config.restart_cooldown_s,
            failures=failures,
        )
        return EXIT_UNHEALTHY

    state["last_restart_epoch"] = current_time
    _write_state(config.state_file, state)
    restarted, detail = _restart(config.restart_command, config.restart_timeout_s)
    _emit("restart", detail=detail, failures=failures, succeeded=restarted)
    return EXIT_RESTARTED if restarted else EXIT_RESTART_FAILED


# ----- CLI --------------------------------------------------------------------------


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least one")
    return parsed


def parse_args(argv: list[str] | None = None) -> WatchdogConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--restart-command", required=True)
    parser.add_argument("--timeout-s", type=_positive_float, default=5.0)
    parser.add_argument("--failure-threshold", type=_positive_int, default=2)
    parser.add_argument("--restart-cooldown-s", type=_positive_float, default=120.0)
    parser.add_argument("--restart-timeout-s", type=_positive_float, default=30.0)
    args = parser.parse_args(argv)
    command = tuple(shlex.split(args.restart_command))
    if not command:
        parser.error("--restart-command must name an executable")
    return WatchdogConfig(
        health_url=args.health_url,
        state_file=args.state_file,
        restart_command=command,
        timeout_s=args.timeout_s,
        failure_threshold=args.failure_threshold,
        restart_cooldown_s=args.restart_cooldown_s,
        restart_timeout_s=args.restart_timeout_s,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"[ERROR] preview engine watchdog: {error}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
