"""MCP phone-remote tools (REMOTE-CTL, issue #10434).

Same documents as ``opendj remote``; tools are built from ``REMOTE_VERBS``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.opendj_cli.remote_cli import (
    REMOTE_VERB_NAMES,
    REMOTE_VERBS,
    RemoteRequestFailed,
    call_verb,
)

LockPath = Callable[[], Path | None]
ToolErrorFn = Callable[[dict[str, Any]], ToolError]
EngineDownFn = Callable[[EngineNotRunning], ToolError]
Action = Callable[[], dict[str, Any]]


def _base_url(lock_path: LockPath, engine_not_running: EngineDownFn) -> str:
    try:
        return resolve_origin(lock_path()).base_url
    except EngineNotRunning as error:
        raise engine_not_running(error) from error


def _invoke(action: Action, tool_error: ToolErrorFn) -> dict[str, Any]:
    try:
        return action()
    except RemoteRequestFailed as error:
        raise tool_error(
            {"error": "request_failed", "status_code": error.status_code, "detail": error.detail}
        ) from error
    except httpx.TransportError as error:
        raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
    except ValueError as error:
        raise tool_error({"error": "usage", "message": str(error)}) from error


def _annotations(verb: object) -> ToolAnnotations:
    from apps.opendj_cli.remote_cli import RemoteVerb

    assert isinstance(verb, RemoteVerb)
    return ToolAnnotations(
        read_only_hint=verb.read_only,
        destructive_hint=verb.destructive,
    )


def register_remote_tools(
    server: MCPServer,
    *,
    lock_path: LockPath,
    tool_error: ToolErrorFn,
    engine_not_running: EngineDownFn,
) -> None:
    def remote_start(port: int | None = None) -> dict[str, Any]:
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: call_verb(base_url, "start", port=port), tool_error)

    def remote_stop() -> dict[str, Any]:
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: call_verb(base_url, "stop"), tool_error)

    def remote_status() -> dict[str, Any]:
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: call_verb(base_url, "status"), tool_error)

    def remote_rotate() -> dict[str, Any]:
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: call_verb(base_url, "rotate"), tool_error)

    def remote_forget(device_id: str) -> dict[str, Any]:
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: call_verb(base_url, "forget", device_id=device_id), tool_error)

    _tools: dict[str, Callable[..., dict[str, Any]]] = {
        "start": remote_start,
        "stop": remote_stop,
        "status": remote_status,
        "rotate": remote_rotate,
        "forget": remote_forget,
    }
    if set(_tools) != set(REMOTE_VERB_NAMES):
        raise RuntimeError("remote MCP tools drifted from REMOTE_VERBS")
    for verb in REMOTE_VERBS:
        server.tool(
            name=f"remote_{verb.name}",
            description=verb.summary,
            annotations=_annotations(verb),
        )(_tools[verb.name])
