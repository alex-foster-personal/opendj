"""Read the analysis queue and order one track's analysis: ``analysis_queue`` and ``analysis_order``.

``analysis_queue`` is ``GET /api/v1/analysis-queue`` (what still needs
analyzing and the one shared refresh job's progress) and, given a
``stable_id``, that track's orders from ``GET /api/v1/analysis-queue/orders/{id}``.
``analysis_order`` is ``POST /api/v1/analysis-queue/orders/{id}/{kind}``, the
route the analysis dots' "re-analyse" action uses, so an order from OJ runs
the same single refresh slot the UI does. ``kind`` is the route's own
:data:`ORDERABLE_ANALYSIS_KINDS`, imported rather than copied, so OJ cannot
offer a kind the engine refuses. Rows are trimmed: ``file_path`` and the job
log stay out of the model's context.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg
from apps.webui.server.routes.ingest_scope import ORDERABLE_ANALYSIS_KINDS

# ----- configuration ------------------------------------------------------
QUEUE_PATH: str = "/api/v1/analysis-queue"
ORDERS_PATH: str = f"{QUEUE_PATH}/orders"

#: The kinds the order route accepts, in a stable order for the JSON schema enum.
ORDER_KINDS: tuple[str, ...] = tuple(sorted(ORDERABLE_ANALYSIS_KINDS))
#: The queue's denominators; ``analyzed + unreachable + too_long + lanes_off + pending == unmapped``.
COUNT_FIELDS: tuple[str, ...] = ("unmapped", "pending", "analyzed", "unreachable", "too_long", "lanes_off")
#: What the model needs of the shared refresh job; the log tail and id lists stay out.
JOB_FIELDS: tuple[str, ...] = ("running", "phase", "steps", "current_step", "step_done", "step_total", "error")
DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 50


# ----- helpers ------------------------------------------------------------
def _track_path(base: str, stable_id: str) -> str:
    return f"{base}/{quote(stable_id, safe='')}"


def _kind_arg(args: dict[str, Any]) -> str:
    kind = text_arg(args, "kind", required=True)
    if kind not in ORDER_KINDS:
        raise ToolFailure("invalid_arguments", f"kind must be one of {', '.join(ORDER_KINDS)}, got {kind!r}")
    return str(kind)


# ----- tools --------------------------------------------------------------
async def _analysis_queue(ctx: ToolContext, args: dict[str, Any]) -> Any:
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT, default=DEFAULT_LIMIT)
    stable_id = text_arg(args, "stable_id")
    page = await engine_json(ctx, "GET", f"{QUEUE_PATH}?limit={limit}")
    items = [{"stable_id": item["stable_id"], "title": item["title"]} for item in page["items"]]
    result: dict[str, Any] = {
        **{name: page[name] for name in COUNT_FIELDS},
        "items": items,
        "more": page["pending"] > len(items),
        "job": {name: page["job"][name] for name in JOB_FIELDS},
        "auto_drain": page["auto"]["enabled"],
    }
    if stable_id is not None:
        orders = await engine_json(ctx, "GET", _track_path(ORDERS_PATH, stable_id))
        result["orders"] = orders["items"]
    return result


async def _analysis_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = str(text_arg(args, "stable_id", required=True))
    kind = _kind_arg(args)
    return await engine_json(ctx, "POST", f"{_track_path(ORDERS_PATH, stable_id)}/{kind}")


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="analysis_queue",
        description=(
            "The analysis queue: counts (unmapped, pending, analyzed, unreachable, too_long, "
            "lanes_off), up to limit pending tracks as {stable_id, title}, more, the shared "
            "analysis job (running, phase, steps, progress, error) and whether auto-drain is on. "
            "Give stable_id to also get that track's orders as {stable_id, kind, phase}. "
            "CLI: opendj api GET /api/v1/analysis-queue; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "stable_id": {"type": "string", "description": "A track whose analysis orders to include."},
            },
        },
        gate="read",
        run=_analysis_queue,
    ),
    ToolSpec(
        name="analysis_order",
        description=(
            "Order one analysis for one track (e.g. re-analyse its beatgrid), through the one shared "
            "analysis job the app's analysis dots use. Answers {stable_id, kind, phase}; follow it "
            "with analysis_queue and that stable_id. A track with no audio file here, or a job "
            "already running for something else, fails engine_error naming 422 or 409. "
            "CLI: opendj api POST /api/v1/analysis-queue/orders/{stable_id}/{kind} "
            "(beatgrid: opendj track analysis-range <stable_id> reanalyze); MCP: library with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": {"type": "string", "description": "The track's stable_id (from library_search)."},
                "kind": {"type": "string", "enum": list(ORDER_KINDS)},
            },
            "required": ["stable_id", "kind"],
        },
        gate="confirm",
        run=_analysis_order,
    ),
)
