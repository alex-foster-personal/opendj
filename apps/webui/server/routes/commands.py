"""AGENT-03 command-order hand-off to the live performance page.

The Web Audio engine is browser-owned. This router therefore never invents an
engine result: it holds the agent request until the open page claims and
executes the order through its normal typed IPC dispatcher.

AGENT-19: ``GET /commands/next?wait_ms=N`` is a LONG POLL. The leader page's
claim loop used to re-ask every 50 ms on a ``setTimeout``, and a hidden tab's
timers are throttled by the browser (Chrome aligns them to 1 s, and a timer
chain to one wake-up per MINUTE after five hidden minutes). A network response
is not throttled, so the engine now holds the claim request open and answers
the moment an order is submitted. Every held answer carries
``x-opendj-order-wait-ms`` so the page can tell a held answer from an older
engine that ignored the parameter.

AGENT-21: ``POST /commands`` never waits forever. An order no leader page has
claimed within ``ORDER_CLAIM_DEADLINE_S`` is withdrawn and answered ``503``
``{"reason": "no_leader"}``: the page on record has stopped claiming (a hidden
tab that lost the mirror lease, a frozen tab), and an agent must learn that
rather than hang. A claimed order still waits for the page's result.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from apps.webui.server.routes.ui_prefs import persist_master_muted
from apps.webui.server.shell_commands import shell_broker

router = APIRouter(prefix="/commands", tags=["agent-commands"])

_ORDER_KEYS = frozenset({"single", "sequence", "parallel", "ramp"})

#: AGENT-19: the longest a claim request may be held open. Below every proxy and
#: webview fetch idle timeout in the path (vite proxy, WKWebView 60 s).
ORDER_WAIT_MAX_MS = 30_000
#: AGENT-19: response header naming the hold the engine honoured, in ms.
ORDER_WAIT_HEADER = "x-opendj-order-wait-ms"
#: AGENT-21: how long a submitted order may sit unclaimed before POST /commands
#: answers 503 no_leader. A live leader claims at once (its claim request is a
#: held long poll); 15 s is 1.5 mirror-lease TTLs, enough for a demoted tab to
#: re-claim the lease and resume claiming. Per app, `app.state` may carry
#: `agent_order_claim_deadline_s` instead (tests use a short one).
ORDER_CLAIM_DEADLINE_S = 15.0
#: AGENT-21: the 503 body's reason when no leader claimed an order in time.
NO_LEADER_REASON = "no_leader"


@dataclass
class _PendingOrder:
    order: dict[str, Any]
    result: asyncio.Future[dict[str, Any]]
    claimed: bool = False
    claimed_event: asyncio.Event | None = None


class _OrderBroker:
    def __init__(self) -> None:
        self.pending: dict[str, _PendingOrder] = {}
        self._waiters: set[asyncio.Future[None]] = set()

    def submit(self, order: dict[str, Any]) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        order_id = uuid4().hex
        result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[order_id] = _PendingOrder(
            order=order, result=result, claimed_event=asyncio.Event()
        )
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        return order_id, result

    def claim(self) -> tuple[str, dict[str, Any]] | None:
        for order_id, pending in self.pending.items():
            if not pending.claimed:
                pending.claimed = True
                if pending.claimed_event is not None:
                    pending.claimed_event.set()
                return order_id, pending.order
        return None

    async def claimed_within(self, order_id: str, deadline_s: float) -> bool:
        """AGENT-21: True once a page claims ``order_id``; False if ``deadline_s`` passes first."""
        pending = self.pending[order_id]
        if pending.claimed:
            return True
        if pending.claimed_event is None:
            raise RuntimeError(f"order {order_id} has no claim event")
        try:
            await asyncio.wait_for(pending.claimed_event.wait(), deadline_s)
        except TimeoutError:
            return pending.claimed
        return True

    async def claim_within(
        self, wait_s: float, asker_is_gone: Callable[[], Awaitable[bool]]
    ) -> tuple[str, dict[str, Any]] | None:
        """AGENT-19: claim now, or hold until an order is submitted or ``wait_s`` passes.

        ``asker_is_gone`` is checked before a held claim is taken: a page that
        closed mid-hold must not swallow an order it can never execute.
        """
        claimed = self.claim()
        if claimed is not None or wait_s <= 0:
            return claimed
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter, wait_s)
        except TimeoutError:
            pass
        finally:
            self._waiters.discard(waiter)
        if await asker_is_gone():
            return None
        return self.claim()

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


def page_is_open(request: Request) -> bool:
    """True only while PUT /state/ui-mirror has a document on record."""
    mirror = getattr(request.app.state, "ui_mirror", None)
    return mirror is not None


def _page_is_open(request: Request) -> bool:
    return page_is_open(request)


async def submit_single_command(request: Request, command: dict[str, Any]) -> dict[str, Any]:
    """Hold ``{kind: "single", payload: command}`` until the open page completes it.

    Caller must have already checked page_is_open. Returns the page's
    ``{steps, mirror_delta}`` document. Does not change POST /commands (still 409).
    """
    order = {"kind": "single", "payload": command}
    broker = _broker(request)
    order_id, result = broker.submit(order)
    try:
        return await result
    finally:
        broker.discard(order_id)


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


def _order_commands(body: dict[str, Any]) -> list[Any]:
    """Every command object a well-formed order carries (ramp's inner one included)."""
    commands: list[Any] = []
    if isinstance(body.get("single"), dict):
        commands.append(body["single"])
    for kind in ("sequence", "parallel"):
        if isinstance(body.get(kind), list):
            commands.extend(body[kind])
    ramp = body.get("ramp")
    if isinstance(ramp, dict) and isinstance(ramp.get("command"), dict):
        commands.append(ramp["command"])
    return commands


def _check_autoplay_commands(body: dict[str, Any]) -> None:
    """AGENT-20: ``{"type": "autoplay", "enabled": <bool>}`` and nothing else.

    The page validates it again before dispatch; checking here too means a
    malformed switch is a 422 at once, not a failed step after a page round trip.
    """
    for command in _order_commands(body):
        if not isinstance(command, dict) or command.get("type") != "autoplay":
            continue
        if set(command) != {"type", "enabled"}:
            raise ValueError(
                f"autoplay takes exactly type and enabled, got {sorted(command)}"
            )
        if not isinstance(command["enabled"], bool):
            raise TypeError(f"autoplay enabled must be boolean, got {command['enabled']!r}")


def _persistable_master_mute(command: Any) -> bool | None:
    """The muted value of a master_mute that may reach disk, else None."""
    if not isinstance(command, dict) or command.get("type") != "master_mute":
        return None
    muted, persist = command.get("muted"), command.get("persist", True)
    if not isinstance(persist, bool):
        raise TypeError("master_mute persist must be boolean")
    return muted if persist and isinstance(muted, bool) else None


def _master_mute_from_order(body: dict[str, Any]) -> bool | None:
    """Last persistable master_mute value in the order, or None if absent.

    A command carrying `persist: false` (the MCP safety rail's prepended mute)
    is skipped: it must never reach the shared ui-prefs.json.
    """
    last: bool | None = None

    def walk(commands: list[Any]) -> None:
        nonlocal last
        for command in commands:
            muted = _persistable_master_mute(command)
            if muted is not None:
                last = muted

    if "single" in body:
        payload = body["single"]
        if isinstance(payload, dict):
            walk([payload])
        return last
    for kind in ("sequence", "parallel"):
        payload = body.get(kind)
        if isinstance(payload, list):
            walk(payload)
    if "ramp" in body:
        payload = body["ramp"]
        if isinstance(payload, dict):
            command = payload.get("command")
            if isinstance(command, dict):
                walk([command])
    return last


@router.post(
    "",
    response_model=None,
    responses={
        503: {
            "description": (
                "AGENT-21: no performance page claimed the order within the claim "
                "deadline (ORDER_CLAIM_DEADLINE_S); body reason is no_leader"
            )
        }
    },
)
async def post_command(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Submit one declared order and wait for the real browser result."""
    if not _page_is_open(request):
        return JSONResponse(status_code=409, content={"client_open": False})
    try:
        order = _order(body)
        _check_autoplay_commands(body)
        muted_to_persist = _master_mute_from_order(body)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if muted_to_persist is not None:
        persist_master_muted(request, muted_to_persist)
    broker = _broker(request)
    order_id, result = broker.submit(order)
    try:
        deadline_s = _claim_deadline_s(request)
        if not await broker.claimed_within(order_id, deadline_s):
            return JSONResponse(
                status_code=503,
                content={
                    "reason": NO_LEADER_REASON,
                    "detail": (
                        f"no performance page claimed the order within {deadline_s:g} s; "
                        "the page on record is not leading (open or focus /performance)"
                    ),
                    "claim_deadline_s": deadline_s,
                },
            )
        return await result
    finally:
        broker.discard(order_id)


def _claim_deadline_s(request: Request) -> float:
    """AGENT-21: the app's claim deadline, else ORDER_CLAIM_DEADLINE_S."""
    configured = getattr(request.app.state, "agent_order_claim_deadline_s", None)
    if configured is None:
        return ORDER_CLAIM_DEADLINE_S
    if not isinstance(configured, int | float) or isinstance(configured, bool) or configured <= 0:
        raise TypeError(f"app.state.agent_order_claim_deadline_s must be a positive number, got {configured!r}")
    return float(configured)


@router.get("/next", response_model=None)
async def next_command(
    request: Request,
    response: Response,
    consumer: str = Query(
        default="performance",
        description=(
            "performance: AGENT-03 orders for the open /performance page; "
            "shell: desktop-shell commands such as apply-update"
        ),
    ),
    wait_ms: int = Query(
        default=0,
        ge=0,
        le=ORDER_WAIT_MAX_MS,
        description=(
            "AGENT-19 long poll, performance consumer only: hold the request up to "
            "this many ms until an order arrives. 0 answers at once (the AGENT-03 "
            f"contract). The honoured hold is echoed in the {ORDER_WAIT_HEADER} header."
        ),
    ),
) -> dict[str, Any] | JSONResponse | None:
    """Let a consumer claim its next command."""
    if consumer == "shell" and wait_ms != 0:
        raise HTTPException(
            status_code=422, detail="wait_ms is only supported for consumer=performance"
        )
    if consumer == "shell":
        claimed = shell_broker(request).claim()
        if claimed is None:
            return None
        command_id, command = claimed
        return {"id": command_id, "kind": "shell", "command": command}
    if consumer not in {"performance"}:
        raise HTTPException(
            status_code=422,
            detail=f"unknown consumer: {consumer!r}; use performance or shell",
        )
    if not _page_is_open(request):
        return JSONResponse(status_code=409, content={"client_open": False})
    claimed = await _broker(request).claim_within(wait_ms / 1000, request.is_disconnected)
    if claimed is None and not _page_is_open(request):
        # The mirror was closed while the request was held.
        return JSONResponse(status_code=409, content={"client_open": False})
    response.headers[ORDER_WAIT_HEADER] = str(wait_ms)
    if claimed is None:
        return None
    order_id, order = claimed
    return {"id": order_id, **order}


def _is_shell_result(body: dict[str, Any]) -> bool:
    return "outcome" in body and "steps" not in body


def _is_performance_result(body: dict[str, Any]) -> bool:
    return isinstance(body.get("steps"), list) and isinstance(body.get("mirror_delta"), dict)


@router.post("/{order_id}/result", status_code=202)
async def complete_command(
    order_id: str, request: Request, body: dict[str, Any]
) -> dict[str, bool]:
    """Resolve a performance order or a shell command result."""
    if _is_shell_result(body) and _is_performance_result(body):
        raise HTTPException(
            status_code=422,
            detail="result cannot mix shell outcome fields with performance steps",
        )
    if _is_shell_result(body):
        status = body.get("status")
        outcome = body.get("outcome")
        if status not in {"succeeded", "failed"}:
            raise HTTPException(status_code=422, detail="shell result requires status")
        if outcome not in {"installed", "no-update", "refused"}:
            raise HTTPException(status_code=422, detail="shell result requires outcome")
        try:
            shell_broker(request).complete(order_id, body)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown shell command") from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": True}
    if not _is_performance_result(body):
        raise HTTPException(status_code=422, detail="result requires steps and mirror_delta")
    try:
        _broker(request).complete(order_id, body)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="unknown command order") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"accepted": True}
