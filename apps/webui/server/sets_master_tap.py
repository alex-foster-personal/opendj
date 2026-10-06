"""The webui app's push channel for a master-mix REC start (SET-12).

Implements :class:`apps.sets.master_tap.MasterTapChannel` on the AGENT-03
order bus: a ``source: master`` start submits ``{"type":
"record_master_tap", "session_id": ...}`` as a single order, which the leader
/performance page claims at once (AGENT-19's held long poll, about 40 ms) and
executes by connecting its master-bus tap. The start route answers only after
the page reports the tap connected, so no audio is lost to a status poll
(CORE, build 15: an agent start lost its first 4.1-4.6 s that way).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import Request

from apps.sets.master_tap import MasterTapUnavailable

from .routes.commands import _broker, _OrderBroker, page_is_open

_log = logging.getLogger(__name__)

#: The command the page's agent-order executor handles (agent-orders.ts).
RECORD_MASTER_TAP_COMMAND = "record_master_tap"
#: Pushed when a start gives up on the tap, so a late attach tears down.
RECORD_MASTER_TAP_CANCEL_COMMAND = "record_master_tap_cancel"
#: How long a withdrawn start keeps its orders on record for late results.
ORPHAN_SETTLE_S = 60.0
#: A live leader claims within ~40 ms; this is the dead-page bound.
CLAIM_DEADLINE_S = 5.0
#: Connecting the tap loads the worklet module once per audio context.
ATTACH_DEADLINE_S = 10.0


class OrderBusMasterTap:
    """Push the attach to the leader /performance page over the order bus."""

    def __init__(self) -> None:
        self._settling: set[asyncio.Task[None]] = set()

    def unavailable_reason(self, request: Request) -> str | None:
        if not page_is_open(request):
            return (
                "no /performance page is open to record the master mix; open it "
                "(Web Audio engine), or record a loopback input"
            )
        return None

    async def attach(self, request: Request, session_id: str) -> None:
        broker = _broker(request)
        order_id, result = broker.submit(
            {"kind": "single", "payload": {"type": RECORD_MASTER_TAP_COMMAND, "session_id": session_id}}
        )
        if not await broker.claimed_within(order_id, CLAIM_DEADLINE_S):
            broker.discard(order_id)
            self._push_cancel(broker, session_id)
            raise MasterTapUnavailable(
                f"no /performance page took the order within {CLAIM_DEADLINE_S:g} s "
                "(the page on record is not leading)"
            )
        try:
            outcome = await asyncio.wait_for(asyncio.shield(result), ATTACH_DEADLINE_S)
        except TimeoutError as exc:
            # The page is still attaching. Keep its order on record so its
            # late result lands (a withdrawn order 404s the page's order loop),
            # and push the cancel, which the page runs right after the attach:
            # the late tap tears itself down instead of streaming (SET-12).
            self._settle_later(broker, order_id, result)
            self._push_cancel(broker, session_id)
            raise MasterTapUnavailable(
                f"the page took the order but did not connect its tap within {ATTACH_DEADLINE_S:g} s"
            ) from exc
        broker.discard(order_id)
        steps = outcome.get("steps") if isinstance(outcome, dict) else None
        if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
            self._push_cancel(broker, session_id)
            raise MasterTapUnavailable(f"the page answered the tap order with {outcome!r}")
        if steps[0].get("status") != "succeeded":
            self._push_cancel(broker, session_id)
            raise MasterTapUnavailable(str(steps[0].get("error") or steps[0]))

    def _push_cancel(self, broker: _OrderBroker, session_id: str) -> None:
        """Submit ``record_master_tap_cancel`` and settle it in the background."""
        order_id, result = broker.submit(
            {"kind": "single", "payload": {"type": RECORD_MASTER_TAP_CANCEL_COMMAND, "session_id": session_id}}
        )
        self._settle_later(broker, order_id, result)

    def _settle_later(self, broker: _OrderBroker, order_id: str, result: asyncio.Future[dict[str, Any]]) -> None:
        async def settle() -> None:
            try:
                await asyncio.wait_for(asyncio.shield(result), ORPHAN_SETTLE_S)
            except TimeoutError:
                _log.warning("master tap order %s got no page result within %s s", order_id, ORPHAN_SETTLE_S)
            finally:
                broker.discard(order_id)

        task = asyncio.get_running_loop().create_task(settle())
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)


__all__ = ["RECORD_MASTER_TAP_CANCEL_COMMAND", "RECORD_MASTER_TAP_COMMAND", "OrderBusMasterTap"]
