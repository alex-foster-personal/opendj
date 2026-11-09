"""AGENT-03 command-order hand-off to the live performance page.

The Web Audio engine is browser-owned. This router therefore never invents an
engine result: it holds the agent request until the open page claims and
executes the order through its normal typed IPC dispatcher.

AGENT-10047: answered orders are timed (submit, claim, result); the last
``ORDER_TIMINGS_MAX`` timings are served at ``GET /commands/timings``.

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
rather than hang. A claimed order waits for the page's result up to
``ORDER_RESULT_DEADLINE_S``, then answers ``503`` ``{"reason": "page_gone"}``.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from apps.webui.server.routes.ui_prefs import persist_master_muted
from apps.webui.server.shell_commands import shell_broker

router = APIRouter(prefix="/commands", tags=["agent-commands"])
MOUNT_UNDER_API_PREFIX = True

#: AGENT-24: ``plan`` arms the page's transition-plan executor and answers at
#: once; ``plan_amend`` aborts, finishes or extends the armed plan.
_ORDER_KEYS = frozenset({"single", "sequence", "parallel", "ramp", "plan", "plan_amend"})

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
#: AGENT-21: how long a claimed order may wait for the page's result before POST
#: answers 503 page_gone. Overridable via ``app.state.agent_order_result_deadline_s``.
ORDER_RESULT_DEADLINE_S = 120.0
#: AGENT-21: the 503 body's reason when the claiming page never answered in time.
PAGE_GONE_REASON = "page_gone"
#: A leased claim must match a mirror lease renewed within this many seconds.
CLAIM_LEASE_MAX_AGE_S = 3.0
#: AGENT-21: the 503 body's reason when no leader claimed an order in time.
NO_LEADER_REASON = "no_leader"
#: Body reason of the 503 an order gets when the engine quits before a page
#: answers it (W43, INSTALL-36).
ENGINE_QUITTING_REASON = "engine_quitting"
#: AGENT-10047: how many completed order timings GET /commands/timings keeps (oldest dropped).
ORDER_TIMINGS_MAX = 200


class OrderBrokerClosed(RuntimeError):
    """The engine is quitting: no page will claim or answer this order."""


@dataclass(frozen=True)
class OrderTiming:
    """AGENT-10047: one answered order's round trip, in ms, from monotonic stamps."""

    type: str
    claim_ms: float
    execute_ms: float
    total_ms: float

    def as_body(self) -> dict[str, float]:
        return {"claim_ms": self.claim_ms, "execute_ms": self.execute_ms, "total_ms": self.total_ms}


@dataclass
class _PendingOrder:
    order: dict[str, Any]
    result: asyncio.Future[dict[str, Any]]
    claimed: bool = False
    claimed_event: asyncio.Event | None = None
    submitted_at: float = 0.0
    claimed_at: float | None = None
    timing: OrderTiming | None = None


def _order_type(order: dict[str, Any]) -> str:
    kind = order.get("kind")
    payload = order.get("payload")
    if kind == "single" and isinstance(payload, dict):
        cmd_type = payload.get("type")
        if isinstance(cmd_type, str) and cmd_type:
            return cmd_type
    if kind is not None:
        return str(kind)
    return "unknown"


def _nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    return ordered[max(1, math.ceil(q * n)) - 1]


def _spread(values: list[float]) -> dict[str, float | None]:
    return {"p50": _nearest_rank(values, 0.5), "p95": _nearest_rank(values, 0.95)}


def _timings_summary(timings: Iterable[OrderTiming], max_orders: int) -> dict[str, Any]:
    items = list(timings)
    count = len(items)
    if count == 0:
        empty = {"p50": None, "p95": None}
        return {
            "count": 0,
            "max_orders": max_orders,
            "total_ms": empty,
            "claim_ms": empty,
            "execute_ms": empty,
            "by_type": {},
        }
    total_vals = [t.total_ms for t in items]
    claim_vals = [t.claim_ms for t in items]
    execute_vals = [t.execute_ms for t in items]
    by_type: dict[str, list[OrderTiming]] = {}
    for t in items:
        by_type.setdefault(t.type, []).append(t)
    by_type_out: dict[str, Any] = {}
    for type_key in sorted(by_type):
        bucket = by_type[type_key]
        b_total = [t.total_ms for t in bucket]
        b_claim = [t.claim_ms for t in bucket]
        b_execute = [t.execute_ms for t in bucket]
        by_type_out[type_key] = {
            "count": len(bucket),
            "total_ms": _spread(b_total),
            "claim_ms": _spread(b_claim),
            "execute_ms": _spread(b_execute),
        }
    return {
        "count": count,
        "max_orders": max_orders,
        "total_ms": _spread(total_vals),
        "claim_ms": _spread(claim_vals),
        "execute_ms": _spread(execute_vals),
        "by_type": by_type_out,
    }


def _fail_closed(pending: _PendingOrder) -> None:
    """Answer an unanswered order with :class:`OrderBrokerClosed` and wake its POST."""
    if not pending.result.done():
        pending.result.set_exception(OrderBrokerClosed("the engine is quitting"))
    if pending.claimed_event is not None:
        pending.claimed_event.set()


def _engine_quitting() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "reason": ENGINE_QUITTING_REASON,
            "detail": "the engine is quitting, so no performance page will run this order",
        },
    )


class _OrderBroker:
    def __init__(self, timings_max: int = ORDER_TIMINGS_MAX) -> None:
        self.pending: dict[str, _PendingOrder] = {}
        self._waiters: set[asyncio.Future[None]] = set()
        self.closed = False
        self.timings: deque[OrderTiming] = deque(maxlen=timings_max)

    def close(self) -> None:
        """W43: the engine is quitting, so every held long poll answers now.

        Left alone, a page's ``wait_ms=20000`` poll holds uvicorn for its whole
        graceful window and is then cancelled, which the page sees as a 500.
        Released here, it answers like a hold that ran out: no order.
        """
        self.closed = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        # An order still waiting for a page gets a terminal answer now, rather
        # than being claimed mid-teardown by a poll that can no longer post
        # its result back (the POST would then hang until it was cancelled).
        for pending in self.pending.values():
            _fail_closed(pending)

    def submit(self, order: dict[str, Any]) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        order_id = uuid4().hex
        result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending = _PendingOrder(
            order=order,
            result=result,
            claimed_event=asyncio.Event(),
            submitted_at=time.monotonic(),
        )
        self.pending[order_id] = pending
        if self.closed:
            _fail_closed(pending)
            return order_id, result
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        return order_id, result

    def claim(self) -> tuple[str, dict[str, Any]] | None:
        if self.closed:
            return None
        for order_id, pending in self.pending.items():
            if not pending.claimed:
                pending.claimed = True
                pending.claimed_at = time.monotonic()
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
        self,
        wait_s: float,
        asker_is_gone: Callable[[], Awaitable[bool]],
        may_claim: Callable[[], bool] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """AGENT-19: claim now, or hold until an order is submitted or ``wait_s`` passes.

        ``asker_is_gone`` is checked before a held claim is taken: a page that
        closed mid-hold must not swallow an order it can never execute.
        ``may_claim``, when set, is checked immediately before every ``claim()``.
        """
        if may_claim is not None and not may_claim():
            return None

        async def _try_claim() -> tuple[str, dict[str, Any]] | None:
            if may_claim is not None and not may_claim():
                return None
            if await asker_is_gone():
                return None
            return self.claim()

        claimed = await _try_claim()
        if claimed is not None or wait_s <= 0 or self.closed:
            return claimed
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter, wait_s)
        except TimeoutError:
            pass
        finally:
            self._waiters.discard(waiter)
        return await _try_claim()

    def complete(self, order_id: str, result: dict[str, Any]) -> None:
        pending = self.pending.get(order_id)
        if pending is None:
            raise KeyError(order_id)
        if not pending.claimed:
            raise RuntimeError(f"order {order_id} was not claimed by a performance page")
        if pending.result.done():
            raise RuntimeError(f"order {order_id} already has a result")
        now = time.monotonic()
        claimed_at = pending.claimed_at if pending.claimed_at is not None else now
        claim_s = claimed_at - pending.submitted_at
        execute_s = now - claimed_at
        total_s = now - pending.submitted_at
        timing = OrderTiming(
            type=_order_type(pending.order),
            claim_ms=round(claim_s * 1000, 3),
            execute_ms=round(execute_s * 1000, 3),
            total_ms=round(total_s * 1000, 3),
        )
        pending.timing = timing
        self.timings.append(timing)
        pending.result.set_result(result)

    def timings_summary(self) -> dict[str, Any]:
        max_orders = self.timings.maxlen if self.timings.maxlen is not None else ORDER_TIMINGS_MAX
        return _timings_summary(self.timings, max_orders)

    def discard(self, order_id: str) -> None:
        self.pending.pop(order_id, None)

    def running(self) -> int:
        """Orders a page has claimed and not yet answered (a ramp can run a minute)."""
        return sum(1 for p in self.pending.values() if p.claimed and not p.result.done())


def _app_broker(app: Any) -> _OrderBroker:
    broker = getattr(app.state, "agent_command_broker", None)
    if broker is None:
        broker = _OrderBroker()
        app.state.agent_command_broker = broker
    if not isinstance(broker, _OrderBroker):
        raise TypeError("app.state.agent_command_broker has an invalid type")
    return broker


def _broker(request: Request) -> _OrderBroker:
    return _app_broker(request.app)


def close_order_broker(app: Any) -> None:
    """W43: release every held ``/commands/next`` long poll; the engine is quitting."""
    _app_broker(app).close()


@dataclass
class OrderPollLiveness:
    """AGENT-22: when the page's order loop last asked for an order.

    The page's loop is the only thing that runs agent orders, and a dead one
    is silent (the mirror keeps publishing). The engine observes the loop
    where its polls land: ``last_at`` moves when a claim arrives and when it
    is answered, and ``waiting`` counts claims held open right now.
    """

    last_at: datetime | None = None
    waiting: int = 0


#: A live loop re-claims within milliseconds of each answer, so a page with no
#: claim held, no order running and no poll for this long has a dead loop.
ORDER_LOOP_STALE_MS = 10_000

OrderLoopState = Literal["polling", "running_order", "stale", "never_polled"]


def order_loop_state(request: Request, now: datetime) -> tuple[str | None, OrderLoopState]:
    """AGENT-22: ``(last_order_poll_at, order_loop_state)`` for the mirror."""
    liveness = order_poll_liveness(request)
    if liveness.last_at is None:
        return None, "never_polled"
    last = liveness.last_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if liveness.waiting > 0:
        return last, "polling"
    if _broker(request).running() > 0:
        return last, "running_order"
    if (now - liveness.last_at).total_seconds() * 1000 > ORDER_LOOP_STALE_MS:
        return last, "stale"
    return last, "polling"


def order_poll_liveness(request: Request) -> OrderPollLiveness:
    liveness = getattr(request.app.state, "order_poll_liveness", None)
    if liveness is None:
        liveness = OrderPollLiveness()
        request.app.state.order_poll_liveness = liveness
    if not isinstance(liveness, OrderPollLiveness):
        raise TypeError("app.state.order_poll_liveness has an invalid type")
    return liveness


def page_is_open(request: Request) -> bool:
    """True only while PUT /state/ui-mirror has a document on record."""
    return page_is_open_on_app(request.app)


def page_is_open_on_app(app: Any) -> bool:
    """:func:`page_is_open` for a caller holding the app, not a request.

    The remote speaker's LAN listener (I18) is a separate app that hands an
    offer to the MAIN app's page, so it has no request of the main app.
    """
    return getattr(app.state, "ui_mirror", None) is not None


def _page_is_open(request: Request) -> bool:
    return page_is_open(request)


def _claim_lease_refusal(request: Request, lease_id: str | None) -> dict[str, Any] | None:
    """None if this asker may claim; else a 409 body.

    Header absent: previous behaviour (CLI, harness, tests that PUT without
    a lease). Header present: live_mirror_lease must match AND have been
    renewed within CLAIM_LEASE_MAX_AGE_S.
    """
    if lease_id is None:
        return None
    from apps.webui.server.routes.state import LEASE_TTL_S, live_mirror_lease

    current = live_mirror_lease(request)
    if current is None:
        return {
            "ok": False,
            "reason": "lease_lapsed",
            "holder": None,
        }
    if current.holder != lease_id:
        return {
            "ok": False,
            "reason": "lease_held",
            "holder": current.holder,
        }
    age_s = LEASE_TTL_S - (current.expires_monotonic - time.monotonic())
    if age_s > CLAIM_LEASE_MAX_AGE_S:
        return {
            "ok": False,
            "reason": "lease_stale",
            "holder": current.holder,
        }
    return None


def _page_gone_detail(deadline_s: float) -> dict[str, Any]:
    return {
        "reason": PAGE_GONE_REASON,
        "detail": (
            f"the claiming page did not answer within {deadline_s:g} s; "
            "the page is gone or stuck"
        ),
        "result_deadline_s": deadline_s,
    }


def _page_gone_response(deadline_s: float) -> JSONResponse:
    return JSONResponse(status_code=503, content=_page_gone_detail(deadline_s))


async def _await_order_result(result: asyncio.Future[dict[str, Any]], deadline_s: float) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(result, deadline_s)
    except TimeoutError as error:
        raise TimeoutError from error


async def submit_single_command(request: Request, command: dict[str, Any]) -> dict[str, Any]:
    """Hold ``{kind: "single", payload: command}`` until the open page completes it.

    Caller must have already checked page_is_open. Returns the page's
    ``{steps, mirror_delta}`` document. Does not change POST /commands (still 409).
    """
    return await submit_single_command_to_app(request.app, command)


async def submit_single_command_to_app(app: Any, command: dict[str, Any]) -> dict[str, Any]:
    """:func:`submit_single_command` for a caller holding the app, not a request."""
    order = {"kind": "single", "payload": command}
    broker = _app_broker(app)
    order_id, result = broker.submit(order)
    deadline_s = _result_deadline_s_on_app(app)
    try:
        return await _await_order_result(result, deadline_s)
    except TimeoutError:
        raise HTTPException(status_code=503, detail=_page_gone_detail(deadline_s)) from None
    except OrderBrokerClosed as error:
        raise HTTPException(
            status_code=503,
            detail={"reason": ENGINE_QUITTING_REASON, "detail": str(error)},
        ) from error
    finally:
        broker.discard(order_id)


def _order(body: dict[str, Any]) -> dict[str, Any]:
    if len(body) != 1:
        raise ValueError(
            "an order must contain exactly one of single, sequence, parallel, ramp, plan, or plan_amend"
        )
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
    if kind in {"ramp", "plan", "plan_amend"} and not isinstance(payload, dict):
        raise ValueError(f"{kind} must be an object")
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
    """AGENT-20: ``{"type": "autoplay", "enabled": <bool>}`` plus PLAY-18's optional ``by_user``.

    The page validates it again before dispatch; checking here too means a
    malformed switch is a 422 at once, not a failed step after a page round trip.
    """
    for command in _order_commands(body):
        if not isinstance(command, dict) or command.get("type") != "autoplay":
            continue
        if set(command) not in ({"type", "enabled"}, {"type", "enabled", "by_user"}):
            raise ValueError(
                f"autoplay takes type, enabled and an optional by_user, got {sorted(command)}"
            )
        if not isinstance(command["enabled"], bool):
            raise TypeError(f"autoplay enabled must be boolean, got {command['enabled']!r}")
        # PLAY-18: the off-needs-a-user rule is USER_ONLY_OFF's, shared with Quantize.


#: DECKUX-39 / PLAY-18: command types whose OFF only a person may ask for, and
#: the name the refusal uses. The page enforces the same rule (agent-orders.ts).
USER_ONLY_OFF = {"quantize": "Quantize", "autoplay": "AutoPlay"}


def user_only_off_refusal(command_type: str) -> str:
    """The 422 detail for an off that carries no user provenance."""
    return (
        f"{command_type} off refused: only a user turns {USER_ONLY_OFF[command_type]} off; "
        "send by_user: true when a person asked for this"
    )


def _check_user_only_offs(body: dict[str, Any]) -> None:
    """DECKUX-39: an agent order may switch these off only with ``by_user: true``.

    Refused here as a 422 (fail fast) rather than reverted on the page: the
    agent learns at once that it must relay a person's ask.
    """
    for command in _order_commands(body):
        if not isinstance(command, dict) or command.get("type") not in USER_ONLY_OFF:
            continue
        if "by_user" in command and not isinstance(command["by_user"], bool):
            raise TypeError(f"{command['type']} by_user must be boolean, got {command['by_user']!r}")
        if command.get("enabled") is False and command.get("by_user") is not True:
            raise ValueError(user_only_off_refusal(command["type"]))


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
                "The order got no result. Body reason no_leader (AGENT-21): no "
                "performance page claimed it within the claim deadline "
                "(ORDER_CLAIM_DEADLINE_S). Body reason page_gone (AGENT-21): the "
                "claiming page did not answer within ORDER_RESULT_DEADLINE_S. Body "
                "reason engine_quitting (INSTALL-36): the engine began quitting "
                "while the order was still waiting for a page."
            )
        }
    },
)
async def post_command(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Submit one declared order and wait for the real browser result.

    AGENT-10047: a successful answer also carries ``timing`` with ``claim_ms``,
    ``execute_ms`` and ``total_ms`` (milliseconds from monotonic stamps).
    """
    if not _page_is_open(request):
        return JSONResponse(status_code=409, content={"client_open": False})
    try:
        order = _order(body)
        _check_autoplay_commands(body)
        _check_user_only_offs(body)
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
        result_deadline_s = _result_deadline_s(request)
        try:
            document = await _await_order_result(result, result_deadline_s)
            pending = broker.pending.get(order_id)
            timing = pending.timing if pending is not None else None
            return {**document, "timing": timing.as_body()} if timing is not None else document
        except TimeoutError:
            return _page_gone_response(result_deadline_s)
    except OrderBrokerClosed:
        return _engine_quitting()
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


def _result_deadline_s(request: Request) -> float:
    """AGENT-21: the app's result deadline, else ORDER_RESULT_DEADLINE_S."""
    return _result_deadline_s_on_app(request.app)


def _result_deadline_s_on_app(app: Any) -> float:
    configured = getattr(app.state, "agent_order_result_deadline_s", None)
    if configured is None:
        return ORDER_RESULT_DEADLINE_S
    if not isinstance(configured, int | float) or isinstance(configured, bool) or configured <= 0:
        raise TypeError(f"app.state.agent_order_result_deadline_s must be a positive number, got {configured!r}")
    return float(configured)


@router.get("/timings", response_model=None)
async def order_timings(request: Request) -> dict[str, Any]:
    """AGENT-10047: count, p50 and p95 (ms) of the last ORDER_TIMINGS_MAX answered orders, overall and by_type."""
    return _broker(request).timings_summary()


@router.get("/next", response_model=None)
async def next_command(
    request: Request,
    response: Response,
    lease_id: str | None = Header(
        default=None,
        alias="x-opendj-lease",
        description=(
            "AGENT-18: the claiming page's client id; refused when another page "
            "holds the mirror lease or the lease is stale."
        ),
    ),
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
    refusal = _claim_lease_refusal(request, lease_id)
    if refusal is not None:
        return JSONResponse(status_code=409, content=refusal)
    liveness = order_poll_liveness(request)
    liveness.last_at = datetime.now(UTC)
    liveness.waiting += 1
    try:
        claimed = await _broker(request).claim_within(
            wait_ms / 1000,
            request.is_disconnected,
            may_claim=lambda: _claim_lease_refusal(request, lease_id) is None,
        )
    finally:
        liveness.waiting -= 1
        liveness.last_at = datetime.now(UTC)
    if claimed is None:
        refusal = _claim_lease_refusal(request, lease_id)
        if refusal is not None:
            return JSONResponse(status_code=409, content=refusal)
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
