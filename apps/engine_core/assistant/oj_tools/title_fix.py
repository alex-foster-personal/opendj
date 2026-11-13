"""Review what a title-fix scan found: ``title_fix_proposals``, ``_apply``, ``_reject``, ``_undo``.

A scan reads tracks whose title carries jammed-in metadata (``Track 8A 124``)
and proposes a clean title and artist. Scans call a hosted model and cost
money, so OJ never starts one: the UI or the CLI does
(``python -m apps.webui.title_fix_cli scan``), and these tools review the
proposals it left. There is deliberately no tool here for the ``/scans`` or
``/key`` routes.

Apply writes the app's own state database only, through the same
``POST /api/v1/title-fix/apply`` the review page uses, as one undoable batch;
rekordbox and the audio files are not written. Undo restores that batch's
originals (``POST /api/v1/title-fix/batches/{id}/undo``) and refuses, writing
nothing, when a track changed since, unless ``skip_conflicts`` is set.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
TITLE_FIX_PATH: str = "/api/v1/title-fix"
PROPOSALS_PATH: str = f"{TITLE_FIX_PATH}/proposals"
APPLY_PATH: str = f"{TITLE_FIX_PATH}/apply"
REJECT_PATH: str = f"{TITLE_FIX_PATH}/reject"
BATCHES_PATH: str = f"{TITLE_FIX_PATH}/batches"

#: The route's own states (``routes/title_fix.py`` ``ProposalStateLit``) plus ``any``.
STATES: tuple[str, ...] = ("pending", "applied", "rejected", "superseded", "stale", "undone", "noop", "any")
#: What one proposal row carries for the model; the route's row has 20-odd fields.
ROW_FIELDS: tuple[str, ...] = (
    "proposal_id",
    "stable_id",
    "state",
    "original_title",
    "original_artist",
    "proposed_title",
    "proposed_artist",
    "tags",
    "batch_id",
)
DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 100
#: The route's own selection cap (``title_fix_core.CFG.MAX_SELECTION``).
MAX_SELECTION: int = 1000


# ----- helpers ------------------------------------------------------------
def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in ROW_FIELDS if name in row}


def _proposal_ids(args: dict[str, Any]) -> list[str]:
    """The required ``proposal_ids``: 1 to MAX_SELECTION distinct non-empty strings."""
    value = args.get("proposal_ids")
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_SELECTION
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise ToolFailure(
            "invalid_arguments",
            f"proposal_ids must be a list of 1 to {MAX_SELECTION} distinct proposal ids, got {value!r}",
        )
    return value


def _state(args: dict[str, Any]) -> str:
    state = text_arg(args, "state") or "pending"
    if state not in STATES:
        raise ToolFailure("invalid_arguments", f"state must be one of {', '.join(STATES)}, got {state!r}")
    return state


def _skip_conflicts(args: dict[str, Any]) -> bool:
    value = args.get("skip_conflicts", False)
    if not isinstance(value, bool):
        raise ToolFailure("invalid_arguments", f"skip_conflicts must be true or false, got {value!r}")
    return value


# ----- tools --------------------------------------------------------------
async def _title_fix_proposals(ctx: ToolContext, args: dict[str, Any]) -> Any:
    state = _state(args)
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT, default=DEFAULT_LIMIT)
    page = await engine_json(ctx, "GET", f"{PROPOSALS_PATH}?{urlencode({'state': state, 'limit': limit})}")
    return {
        "items": [_compact(row) for row in page["items"]],
        "total": page["total"],
        "counts_by_state": page["counts_by_state"],
    }


async def _title_fix_apply(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = {"proposal_ids": _proposal_ids(args)}
    return (await engine_call(ctx, "POST", APPLY_PATH, body)).body


async def _title_fix_reject(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = {"proposal_ids": _proposal_ids(args)}
    return (await engine_call(ctx, "POST", REJECT_PATH, body)).body


async def _title_fix_undo(ctx: ToolContext, args: dict[str, Any]) -> Any:
    batch_id = str(text_arg(args, "batch_id", required=True))
    body = {"skip_conflicts": _skip_conflicts(args)}
    return (await engine_call(ctx, "POST", f"{BATCHES_PATH}/{quote(batch_id, safe='')}/undo", body)).body


_IDS = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "maxItems": MAX_SELECTION,
    "uniqueItems": True,
    "description": "proposal_id values from title_fix_proposals.",
}
_OUTCOME = "Answers {batch_id, applied, skipped: [{proposal_id, reason}], warning}."
_SELECTION = {"type": "object", "properties": {"proposal_ids": _IDS}, "required": ["proposal_ids"]}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="title_fix_proposals",
        description=(
            "List the title fixes a scan proposed: tracks whose title carries jammed-in metadata, "
            "with the current and proposed title and artist. Scans are started from the app or CLI, "
            "never by OJ. Answers {items, total, counts_by_state}; each item has proposal_id, "
            "stable_id, state, original_title, original_artist, proposed_title, proposed_artist, "
            "tags, batch_id. CLI: python -m apps.webui.title_fix_cli proposals --state pending; "
            "MCP: app_state with GET /api/v1/title-fix/proposals."
        ),
        parameters={
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": list(STATES), "default": "pending"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
        },
        gate="read",
        run=_title_fix_proposals,
    ),
    ToolSpec(
        name="title_fix_apply",
        description=(
            "Apply the chosen pending title fixes as one undoable batch (the app's library only; "
            "rekordbox and the audio files are not written). A proposal whose track changed since "
            f"the scan is skipped as stale. {_OUTCOME} Keep batch_id for title_fix_undo. "
            "CLI: python -m apps.webui.title_fix_cli apply <proposal_id>... --confirm; "
            "MCP: library POST /api/v1/title-fix/apply."
        ),
        parameters=_SELECTION,
        gate="confirm",
        run=_title_fix_apply,
    ),
    ToolSpec(
        name="title_fix_reject",
        description=(
            "Reject the chosen pending title fixes; no track is changed. "
            f"{_OUTCOME} CLI: python -m apps.webui.title_fix_cli reject <proposal_id>...; "
            "MCP: library POST /api/v1/title-fix/reject."
        ),
        parameters=_SELECTION,
        gate="confirm",
        run=_title_fix_reject,
    ),
    ToolSpec(
        name="title_fix_undo",
        description=(
            "Undo one applied title-fix batch, restoring each track's original title and artist. "
            "If a track changed since the apply it fails engine_error naming 409 and nothing is "
            f"undone; pass skip_conflicts to undo the rest. {_OUTCOME} An unknown batch fails "
            "engine_error naming 404. CLI: python -m apps.webui.title_fix_cli undo <batch_id> "
            "--confirm; MCP: library POST /api/v1/title-fix/batches/{batch_id}/undo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "The batch_id title_fix_apply answered."},
                "skip_conflicts": {"type": "boolean", "default": False},
            },
            "required": ["batch_id"],
        },
        gate="confirm",
        run=_title_fix_undo,
    ),
)
