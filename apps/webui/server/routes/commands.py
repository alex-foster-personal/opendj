"""AGENT-03 command-order hand-off to the live performance page.

The Web Audio engine is browser-owned. This router therefore never invents an
engine result: it holds the agent request until the open page claims and
executes the order through its normal typed IPC dispatcher.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/commands", tags=["agent-commands"])

_ORDER_KEYS = frozenset({"single", "sequence", "parallel", "ramp"})


@dataclass
class _PendingOrder:
    order: dict[str, Any]
    result: asyncio.Future[dict[str, Any]]
    claimed: bool = False


class _OrderBroker:
    def __init__(self) -> None:
        self.pending: dict[str, _PendingOrder] = {}

    def submit(self, order: dict[str, Any]) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        order_id = uuid4().hex
        result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[order_id] = _PendingOrder(order=order, result=result)
        return order_id, result

    def claim(self) -> tuple[str, dict[str, Any]] | None:
        for order_id, pending in self.pending.items():
            if not pending.claimed:
                pending.claimed = True
                return order_id, pending.order
        return None

    def complete(self, order_id: str, result: dict[str, Any]) -> None:
        pending = self.pending.get(order_id)
        if pending is None:
            raise KeyError(order_id)
        if not pending.claimed:
            raise RuntimeError(f"order {order_id} was not claimed by a performance page")
        if pending.result.done():
            raise RuntimeError(f"order {order_id} already has a result")
        pending.result.set_result(result)

    def discard(self, order_id: str) -> None:
        self.pending.pop(order_id, None)


def _broker(request: Request) -> _OrderBroker:
    broker = getattr(request.app.state, "agent_command_broker", None)
    if broker is None:
        broker = _OrderBroker()
        request.app.state.agent_command_broker = broker
    if not isinstance(broker, _OrderBroker):
        raise TypeError("app.state.agent_command_broker has an invalid type")
    return broker


def _page_is_open(request: Request) -> bool:
    mirror = getattr(request.app.state, "ui_mirror", None)
    return mirror is not None


def _order(body: dict[str, Any]) -> dict[str, Any]:
    if len(body) != 1:
        raise ValueError("an order must contain exactly one of single, sequence, parallel, or ramp")
    kind, payload = next(iter(body.items()))
    if kind not in _ORDER_KEYS:
        raise ValueError(f"unknown order kind: {kind}")
    if kind == "single" and not isinstance(payload, dict):
        raise ValueError("single must be a command object")
    if kind in {"sequence", "parallel"} and (
        not isinstance(payload, list)
        or not payload
        or not all(isinstance(step, dict) for step in payload)
    ):
        raise ValueError(f"{kind} must be a non-empty list of command objects")
    if kind == "ramp" and not isinstance(payload, dict):
        raise ValueError("ramp must be an object")
    return {"kind": kind, "payload": payload}


@router.post("", response_model=None)
async def post_command(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Submit one declared order and wait for the real browser result."""
    if not _page_is_open(request):
        return JSONResponse(status_code=409, content={"client_open": False})
    try:
        order = _order(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    broker = _broker(request)
    order_id, result = broker.submit(order)
    try:
        return await result
    finally:
        broker.discard(order_id)


@router.get("/next", response_model=None)
async def next_command(request: Request) -> dict[str, Any] | JSONResponse | None:
    """Let the sole open performance page claim its next agent order."""
    if not _page_is_open(request):
        return JSONResponse(status_code=409, content={"client_open": False})
    claimed = _broker(request).claim()
    if claimed is None:
        return None
    order_id, order = claimed
    return {"id": order_id, **order}


@router.post("/{order_id}/result", status_code=202)
async def complete_command(
    order_id: str, request: Request, body: dict[str, Any]
) -> dict[str, bool]:
    """Resolve an order with page-produced per-step statuses and mirror delta."""
    if not isinstance(body.get("steps"), list) or not isinstance(body.get("mirror_delta"), dict):
        raise HTTPException(status_code=422, detail="result requires steps and mirror_delta")
    try:
        _broker(request).complete(order_id, body)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="unknown command order") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"accepted": True}
