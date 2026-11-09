"""Shared pytest helpers for three-way agent parity (CLI verb, HTTP order, MCP tool)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from httpx import ASGITransport, AsyncClient

from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations
from apps.opendj_cli.verbs import parse_invocation
from apps.webui.server.routes.commands import router as commands_router
from apps.webui.server.routes.state import router as state_router
from fastapi import FastAPI


def _unwrap_order_command(order: dict[str, Any]) -> dict[str, Any]:
    """The bus command inside an AGENT-03 order body."""
    if set(order.keys()) == {"single"}:
        payload = order["single"]
        if not isinstance(payload, dict):
            raise TypeError("order single payload is not a dict")
        return payload
    raise AssertionError(f"parity helper expects a single-command order, got keys {sorted(order)}")


def _unwrap_claimed_command(claimed: dict[str, Any]) -> dict[str, Any]:
    kind = claimed.get("kind")
    payload = claimed.get("payload")
    if kind == "single" and isinstance(payload, dict):
        return payload
    raise AssertionError(f"claimed order is not single, kind={kind!r}")


class CapturedBus:
    """Records bus commands submitted through a test ``run_order`` callback."""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []

    def run_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.orders.append(order)
        return {"ok": True}

    def single_command(self) -> dict[str, Any]:
        if len(self.orders) != 1:
            raise AssertionError(f"expected one order, got {len(self.orders)}")
        return _unwrap_order_command(self.orders[0])


def _tool_annotations(server: MCPServer, tool_name: str) -> ToolAnnotations | None:
    for tool in asyncio.run(server.list_tools()):
        if tool.name == tool_name:
            return tool.annotations
    return None


def mcp_tool_command(
    register_fn: Callable[..., None],
    tool_name: str,
    *,
    mutating: bool = True,
    register_kwargs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Invoke ``tool_name`` like the MCP SDK does and return the one bus command sent."""
    bus = CapturedBus()
    server = MCPServer("parity-test")
    extra = register_kwargs or {}
    register_fn(server, run_order=bus.run_order, **extra)
    annotations = _tool_annotations(server, tool_name)
    if mutating and annotations is not None and annotations.read_only_hint:
        raise AssertionError(f"tool {tool_name!r} is read-only; refusing a mutating parity check that would be a no-op")
    asyncio.run(server.call_tool(tool_name, kwargs))
    return bus.single_command()


def _commands_app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(commands_router, prefix="/api/v1")
    app.state.agent_order_claim_deadline_s = 0.25
    app.state.agent_order_result_deadline_s = 0.25
    return app


async def _open_leader(client: AsyncClient) -> None:
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


async def http_order_command_async(client: AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    """POST an order and return the bus command the leader page received."""
    await _open_leader(client)
    order_task = asyncio.create_task(client.post("/api/v1/commands", json=body))
    claimed: dict[str, Any] | None = None
    for _ in range(200):
        await asyncio.sleep(0.01)
        nxt = await client.get("/api/v1/commands/next")
        assert nxt.status_code == 200
        if nxt.json() is not None:
            claimed = nxt.json()
            break
    assert claimed is not None, "the open page was never offered the order"
    await client.post(
        f"/api/v1/commands/{claimed['id']}/result",
        json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
    )
    response = await order_task
    assert response.status_code == 200
    return _unwrap_claimed_command(claimed)


def http_order_command(client: AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(http_order_command_async(client, body))


def _dict_diff(left: dict[str, Any], right: dict[str, Any], *, left_name: str, right_name: str) -> str | None:
    keys = set(left) | set(right)
    parts: list[str] = []
    for key in sorted(keys):
        if key not in left:
            parts.append(f"key {key!r} only on {right_name}")
        elif key not in right:
            parts.append(f"key {key!r} only on {left_name}")
        elif left[key] != right[key]:
            parts.append(f"key {key!r}: {left_name}={left[key]!r} {right_name}={right[key]!r}")
    return "; ".join(parts) if parts else None


def assert_three_way_parity(
    verb_tokens: list[str],
    tool: str,
    tool_args: dict[str, Any],
    http_body: dict[str, Any],
    *,
    register_fn: Callable[..., None],
    register_kwargs: dict[str, Any] | None = None,
) -> None:
    """CLI, MCP and HTTP must emit the same bus command dict."""
    cli = parse_invocation(verb_tokens).command
    mcp = mcp_tool_command(register_fn, tool, register_kwargs=register_kwargs, mutating=True, **tool_args)

    async def run_http() -> dict[str, Any]:
        app = _commands_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await http_order_command_async(client, http_body)

    http = asyncio.run(run_http())
    command_type = cli.get("type")
    assert isinstance(command_type, str), "cli command has no type"
    assert command_type in VERBS, f"unknown verb command type {command_type!r}"

    if cli != mcp:
        detail = _dict_diff(cli, mcp, left_name="cli", right_name="mcp") or "commands differ"
        raise AssertionError(f"MCP parity failed: {detail}")
    if cli != http:
        detail = _dict_diff(cli, http, left_name="cli", right_name="http") or "commands differ"
        raise AssertionError(f"HTTP parity failed: {detail}")
