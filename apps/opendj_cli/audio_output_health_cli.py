"""``opendj audio_output_health`` / ``audio_switch_output``: issue #923 HTTP parity."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, exit_for_status, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning

OUTPUT_HEALTH_PATH = "/api/v1/audio/output-health"
SWITCH_OUTPUT_PATH = "/api/v1/audio/switch-output"
REQUEST_TIMEOUT_S = 3.0


def _fail(as_json: bool, code: str, message: str, exit_code: int) -> int:
    if as_json:
        print(
            json.dumps(
                {"error": {"code": code, "message": message}, "exit_code": exit_code},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj: {message}", file=sys.stderr)
    return exit_code


def _request(
    method: str,
    path: str,
    *,
    lock: Path | None,
) -> dict[str, Any]:
    target = resolve_backend_base_url(lock_path=lock)
    url = f"{target.base_url.rstrip('/')}{path}"
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, url)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"{method} {path} returned {response.status_code}: {response.text}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"{method} {path} returned non-object JSON")
    return payload


def _fault_line(fault: object) -> str:
    if fault is None:
        return "none"
    if isinstance(fault, dict):
        return f"{fault.get('kind')} ({fault.get('stage')}): {fault.get('message')}"
    raise TypeError(f"master_pin_fault must be an object or null, got {fault!r}")


def run_health(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    lock: Path | None = None,
) -> int:
    if rest:
        return _fail(
            as_json,
            "usage",
            f"audio_output_health takes no arguments, got {rest[0]!r}",
            EXIT_FAILED,
        )
    try:
        payload = _request("GET", OUTPUT_HEALTH_PATH, lock=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except (httpx.HTTPError, RuntimeError, TypeError) as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"verdict: {payload.get('verdict')}\n"
            f"device_delivering: {payload.get('device_delivering')}\n"
            f"reason: {payload.get('reason')}\n"
            f"master_pin_fault: {_fault_line(payload.get('master_pin_fault'))}"
        )
    return EXIT_CONFIRMED


def run_switch_output(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    lock: Path | None = None,
) -> int:
    if "--confirm" not in rest:
        return _fail(
            as_json,
            "usage",
            "audio_switch_output refuses without --confirm; "
            "pass --confirm to cycle the default output",
            EXIT_FAILED,
        )
    extra = [token for token in rest if token != "--confirm"]
    if extra:
        return _fail(
            as_json,
            "usage",
            f"audio_switch_output accepts only --confirm, got {extra[0]!r}",
            EXIT_FAILED,
        )
    try:
        target = resolve_backend_base_url(lock_path=lock)
    except UsageError as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    url = f"{target.base_url.rstrip('/')}{SWITCH_OUTPUT_PATH}"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.post(url)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = {"raw": response.text}
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"status: {response.status_code}")
        print(response.text)
    return exit_for_status(response.status_code)
