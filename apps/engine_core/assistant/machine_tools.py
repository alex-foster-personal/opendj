"""Machine tools for the assistant: output volume and USB volumes.

Each tool calls the engine's public ``/api/v1`` route at ``ctx.base_url``
over HTTP, so the assistant can do nothing an HTTP client could not. Reads
are ``read``-gated; anything that changes the machine is ``confirm``-gated.
A non-2xx answer becomes :class:`ToolFailure` carrying the route's own
``detail.code``.
"""

from __future__ import annotations

from typing import Any

import httpx

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec

TOOL_HTTP_TIMEOUT_S: float = 45.0
_VOLUME_PATH = "/api/v1/system/audio/volume"
_USB_VOLUMES_PATH = "/api/v1/usb/volumes"
_USB_EJECT_PATH = "/api/v1/usb/eject"


# ----- HTTP -----------------------------------------------------------------


def _failure(response: httpx.Response) -> ToolFailure:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        message = detail.get("message") or detail.get("reason") or detail["code"]
        return ToolFailure(detail["code"], str(message))
    return ToolFailure(f"http_{response.status_code}", response.text[:500])


async def _call(ctx: ToolContext, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    try:
        async with httpx.AsyncClient(base_url=ctx.base_url, timeout=TOOL_HTTP_TIMEOUT_S) as client:
            response = await client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise ToolFailure("engine_unreachable", f"{method} {path}: {exc!r}") from exc
    if not response.is_success:
        raise _failure(response)
    return response.json()


# ----- tools ----------------------------------------------------------------


async def _volume_get(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return await _call(ctx, "GET", _VOLUME_PATH)


async def _volume_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    volume = args.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
        raise ToolFailure("invalid_arguments", f"volume must be an integer percent 0-100, got {volume!r}")
    return await _call(ctx, "PUT", _VOLUME_PATH, {"volume": volume})


async def _usb_list(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return await _call(ctx, "GET", _USB_VOLUMES_PATH)


async def _usb_eject(ctx: ToolContext, args: dict[str, Any]) -> Any:
    volume_id = args.get("volume_id")
    if not isinstance(volume_id, str) or not volume_id:
        raise ToolFailure("invalid_arguments", "volume_id must be a non-empty string from usb_list")
    listing = await _call(ctx, "GET", _USB_VOLUMES_PATH)
    volume = next((row for row in listing["volumes"] if row["id"] == volume_id), None)
    if volume is None:
        raise ToolFailure("unknown_volume", f"no present volume has id {volume_id!r}")
    if volume["mount_path"] is None:
        raise ToolFailure("volume_not_mounted", f"volume {volume_id!r} is not mounted")
    return await _call(ctx, "POST", _USB_EJECT_PATH, {"mount_path": volume["mount_path"], "volume_id": volume_id})


_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

MACHINE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="system_volume_get",
        description="Read the computer's master output volume (integer percent 0-100) and mute state.",
        parameters=_NO_ARGS,
        gate="read",
        run=_volume_get,
    ),
    ToolSpec(
        name="system_volume_set",
        description="Set the computer's master output volume to an integer percent 0-100; returns the value read back.",
        parameters={
            "type": "object",
            "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["volume"],
            "additionalProperties": False,
        },
        gate="confirm",
        run=_volume_set,
    ),
    ToolSpec(
        name="usb_list",
        description="List mounted USB and external volumes with their id, name, role and whether they hold DJ music.",
        parameters=_NO_ARGS,
        gate="read",
        run=_usb_list,
    ),
    ToolSpec(
        name="usb_eject",
        description="Eject one removable USB volume by the id usb_list returned. Refuses unknown or non-removable volumes.",
        parameters={
            "type": "object",
            "properties": {"volume_id": {"type": "string", "minLength": 1}},
            "required": ["volume_id"],
            "additionalProperties": False,
        },
        gate="confirm",
        run=_usb_eject,
    ),
)

__all__ = ["MACHINE_TOOLS"]
