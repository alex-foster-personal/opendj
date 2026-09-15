"""``opendj status``: lock-file origin, health, and build-info (MCP ``status`` parity)."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.engine_status import build_engine_status
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin


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


def _text_summary(document: dict[str, Any]) -> str:
    health = document.get("health")
    status = health.get("status") if isinstance(health, dict) else health
    return (
        f"origin: {document.get('origin')}\n"
        f"pid: {document.get('pid')}\n"
        f"role: {document.get('role')}\n"
        f"health: {status}"
    )


def run(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    lock: Path | None = None,
) -> int:
    if rest:
        return _fail(
            as_json,
            "usage",
            f"status takes no arguments, got {rest[0]!r}",
            EXIT_FAILED,
        )
    try:
        origin = resolve_origin(lock)
    except EngineNotRunning as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    document = build_engine_status(origin)
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(_text_summary(document))
    return EXIT_CONFIRMED
