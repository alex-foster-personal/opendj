"""MCP audio-output tools (AUDIO-DEVICE-03..05).

Agent parity for the I/O panel's speaker-link surface: read the main-output
health (shell link check plus the acoustic proof), list the inputs the
acoustic proof may use, and run the Reconnect speaker shell step. Split out of
:mod:`apps.opendj_cli.mcp_server`, which sits at the repo's 600-line ceiling.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_safety import ENABLE_DESTRUCTIVE_ENV, destructive_enabled, engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
AUDIO_PATH = "/api/v1/audio"
READ_TIMEOUT_S = 5.0
# The engine waits up to 50 s on the shell's blueutil reconnect.
RECONNECT_TIMEOUT_S = 60.0


def register_audio_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``audio_output_health``, ``audio_input_devices`` and ``audio_reconnect_speaker``."""

    def call(method: str, path: str, timeout: float) -> dict[str, Any]:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(method, f"{origin.base_url}{AUDIO_PATH}{path}")
        except httpx.TransportError as error:
            raise tool_error(
                {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
            ) from error
        try:
            body: Any = response.json()
        except ValueError:
            raise tool_error(
                {"error": "engine_bad_response", "status_code": response.status_code, "text": response.text[:500]}
            ) from None
        if not response.is_success:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise tool_error(
                {
                    "error": engine_error_code(detail, response.status_code),
                    "status_code": response.status_code,
                    "detail": detail or body,
                }
            )
        if not isinstance(body, dict):
            raise tool_error({"error": "engine_bad_response", "status_code": response.status_code, "body": body})
        return body

    @server.tool(annotations=_READ_ONLY)
    def audio_output_health() -> dict[str, Any]:
        """The main output's delivery verdict (AUDIO-DEVICE-01..04).

        Carries the shell link check (DeviceIsAlive, IsRunningSomewhere, the
        IO-proc sample clock against host time) and the latest acoustic proof,
        which turns an ``ok`` into ``not_delivering`` when a wired or USB mic
        did not hear the master bus.
        """
        return call("GET", "/output-health", READ_TIMEOUT_S)

    @server.tool(annotations=_READ_ONLY)
    def audio_input_devices() -> dict[str, Any]:
        """CoreAudio inputs with transport; only ``acoustic_proof_allowed`` ones may be the proof mic."""
        return call("GET", "/input-devices", READ_TIMEOUT_S)

    @server.tool(annotations=_DESTRUCTIVE)
    def audio_reconnect_speaker() -> dict[str, Any]:
        """Disconnect and reconnect the main Bluetooth output, then make it default (AUDIO-DEVICE-05).

        The I/O panel's Reconnect speaker button runs this same shell step and
        then recreates the room AudioContext; from here the page re-opens its
        output on the resulting device change. Interrupts the room audio for
        about 5 s, so it is gated like the other destructive tools.
        """
        if not destructive_enabled():
            raise tool_error(
                {"error": "destructive_blocked", "message": f"set {ENABLE_DESTRUCTIVE_ENV}=1 to reconnect the speaker"}
            )
        return call("POST", "/reconnect-speaker", RECONNECT_TIMEOUT_S)
