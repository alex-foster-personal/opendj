"""One track's stems: ``stems_state``, ``stems_generate`` and ``stems_job``.

``stems_state`` names what the deck's stem row shows (local, cloud, fetching,
error or none) and never starts a download. ``stems_generate`` starts a real
separation, which spends compute (a torch process on this machine or a paid
Modal GPU), so it is ``confirm``-gated. Before it posts, it reads
``GET /api/v1/stems/tiers`` and refuses a tier this engine would not run, so
the model gets the runnable tiers back instead of a 400, 409 or 503 from the
engine. ``stems_job`` polls the job ``stems_generate`` answered.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import text_arg

# ----- configuration ------------------------------------------------------
TIERS_PATH: str = "/api/v1/stems/tiers"
GENERATE_PATH: str = "/api/v1/stems/generate"
JOBS_PATH: str = "/api/v1/stems/jobs"
TRACKS_PATH: str = "/api/v1/tracks"
#: ``availability`` of a tier the engine will separate with (``apps.stems.tiers``).
AVAILABLE: str = "AVAILABLE"


# ----- helpers ------------------------------------------------------------
def _segment(value: str) -> str:
    return quote(value, safe="")


def _refusal(tier: str, tiers: list[dict[str, Any]]) -> str | None:
    """Why the engine would not run ``tier``, naming the runnable tiers; None when it would."""
    runnable = [row["key"] for row in tiers if row["availability"] == AVAILABLE and row["runnable_here"]]
    if tier in runnable:
        return None
    listed = {row["key"]: row for row in tiers}
    if tier not in listed:
        reason = "is not a tier this engine lists"
    elif listed[tier]["availability"] != AVAILABLE:
        reason = f"is {listed[tier]['availability']}: {listed[tier]['unavailable_because']}"
    elif listed[tier]["availability"] == AVAILABLE:
        reason = f"cannot run here: {listed[tier]['not_runnable_because']}"
    return f"tier {tier!r} {reason}. Allowed tiers: {', '.join(runnable) or 'none'}"


# ----- tools --------------------------------------------------------------
async def _stems_state(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = text_arg(args, "stable_id", required=True)
    return (await engine_call(ctx, "GET", f"{TRACKS_PATH}/{_segment(str(stable_id))}/stems/state")).body


async def _stems_generate(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = text_arg(args, "stable_id", required=True)
    tier = str(text_arg(args, "tier", required=True)).strip().upper()
    refusal = _refusal(tier, await engine_json(ctx, "GET", TIERS_PATH))
    if refusal is not None:
        raise ToolFailure("invalid_arguments", refusal)
    return (await engine_call(ctx, "POST", GENERATE_PATH, {"stable_id": stable_id, "tier": tier})).body


async def _stems_job(ctx: ToolContext, args: dict[str, Any]) -> Any:
    job_id = text_arg(args, "job_id", required=True)
    return (await engine_call(ctx, "GET", f"{JOBS_PATH}/{_segment(str(job_id))}")).body


_STABLE_ID = {"type": "string", "description": "The track's stable_id (from library_search)."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="stems_generate",
        description=(
            "Start a real stem separation for one track at one tier; it spends compute "
            "(a local torch process or a paid Modal GPU), so the user approves it first. "
            "A tier the engine does not list or cannot run here fails invalid_arguments "
            "naming the allowed tiers, and nothing is started. Answers {job_id, tier, "
            "stable_id, poll, log}; poll with stems_job. "
            "CLI: opendj api POST /api/v1/stems/generate (tiers: opendj api GET /api/v1/stems/tiers)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": _STABLE_ID,
                "tier": {"type": "string", "description": "A tier key from GET /api/v1/stems/tiers, e.g. M."},
            },
            "required": ["stable_id", "tier"],
        },
        gate="confirm",
        run=_stems_generate,
    ),
    ToolSpec(
        name="stems_job",
        description=(
            "Live state of one stems_generate job: running, done or failed, its returncode "
            "and the tail of its log. An unknown job_id fails engine_error naming 404 "
            "(jobs do not survive an engine restart). "
            "CLI: opendj api GET /api/v1/stems/jobs/{job_id}; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "The job_id stems_generate answered."}},
            "required": ["job_id"],
        },
        gate="read",
        run=_stems_job,
    ),
    ToolSpec(
        name="stems_state",
        description=(
            "One track's stem state, as the deck's stem row shows it: local, cloud, fetching "
            "(with progress), error (with code and message) or none. Reading it never starts "
            "a download. "
            "CLI: opendj api GET /api/v1/tracks/{stable_id}/stems/state; MCP: app_state with that path."
        ),
        parameters={"type": "object", "properties": {"stable_id": _STABLE_ID}, "required": ["stable_id"]},
        gate="read",
        run=_stems_state,
    ),
)
