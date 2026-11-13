"""Read and edit smartlists: list, tracks, create, update rule.

Every tool calls the route the smartlist editor calls (``/api/v1/smartlists``),
so OJ, the CLI (``opendj api``) and MCP (``library``) share one write path.
Reads are gate ``read``; both writes are gate ``confirm``. Delete is left out on
purpose: the generic ``library_write`` still reaches it, behind its confirm.

The rule object is passed to the engine unchanged; the engine validates it.
A rule the engine refuses (its 422) fails ``invalid_arguments`` carrying the
engine's own detail, so the model can fix the rule and call again.

``smartlist_update`` needs ``If-Match``: it reads the smartlist's current ETag
itself (``GET .../{id}``) and sends it, so the model never handles an etag. A
change landing between that read and the write is the engine's 409, reported
as ``engine_error`` naming 409 and never retried. Rows come back trimmed to
:data:`SMARTLIST_FIELDS` and :data:`TRACK_FIELDS`: a full track row is about 3 KB.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
SMARTLISTS_PATH: str = "/api/v1/smartlists"

#: What one smartlist row carries for the model; ``rule`` is kept so it can be edited.
SMARTLIST_FIELDS: tuple[str, ...] = ("id", "name", "rule", "rule_summary", "order_by", "count")
#: What one smartlist member row carries for the model.
TRACK_FIELDS: tuple[str, ...] = ("stable_id", "title", "artist", "bpm", "key", "rating", "genre", "duration_ms")
DEFAULT_TRACKS_LIMIT: int = 50
#: The tracks route's own bound (``limit`` le=10000) is wider; 200 rows stay small.
MAX_TRACKS_LIMIT: int = 200
#: The status the smartlist routes answer for a rule they refuse (``SMARTLIST_RULE_INVALID``).
RULE_REFUSED_STATUS: int = 422


# ----- helpers ------------------------------------------------------------
def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: row[name] for name in fields if name in row}


def _smartlist_path(args: dict[str, Any], suffix: str = "") -> str:
    smartlist_id = text_arg(args, "smartlist_id", required=True)
    return f"{SMARTLISTS_PATH}/{quote(str(smartlist_id), safe='')}{suffix}"


def _rule_arg(args: dict[str, Any]) -> dict[str, Any]:
    """The rule object as given (never reshaped); the engine judges its contents."""
    rule = args.get("rule")
    if not isinstance(rule, dict) or not rule:
        raise ToolFailure("invalid_arguments", f"rule must be a non-empty rule object, got {rule!r}")
    return rule


def _write_body(args: dict[str, Any], **extra: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"rule": _rule_arg(args), "order_by": text_arg(args, "order_by"), **extra}
    return {name: value for name, value in body.items() if value is not None}


async def _write(ctx: ToolContext, method: str, path: str, body: dict[str, Any], *, if_match: str | None = None) -> Any:
    """One smartlist write; a refused rule becomes ``invalid_arguments`` with the engine's detail."""
    try:
        answer = await engine_call(ctx, method, path, body, if_match=if_match)
    except ToolFailure as exc:
        if exc.code == "engine_error" and f" -> {RULE_REFUSED_STATUS}: " in exc.message:
            raise ToolFailure("invalid_arguments", f"the engine refused this smartlist: {exc.message}") from exc
        raise
    return {**_project(answer.body, SMARTLIST_FIELDS), "etag": answer.etag}


# ----- read tools ---------------------------------------------------------
async def _smartlist_list(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    rows = await engine_json(ctx, "GET", f"{SMARTLISTS_PATH}?include_counts=true")
    return {"items": [_project(row, SMARTLIST_FIELDS) for row in rows], "total": len(rows)}


async def _smartlist_tracks(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _smartlist_path(args, "/tracks")
    limit = int_arg(args, "limit", low=1, high=MAX_TRACKS_LIMIT, default=DEFAULT_TRACKS_LIMIT)
    page = await engine_json(ctx, "GET", f"{path}?{urlencode({'limit': limit})}")
    return {
        "smartlist_id": page["smartlist_id"],
        "name": page["name"],
        "rule_summary": page["rule_summary"],
        "total": page["total"],
        "items": [_project(row, TRACK_FIELDS) for row in page["tracks"]],
    }


# ----- write tools --------------------------------------------------------
async def _smartlist_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = _write_body(args, name=text_arg(args, "name", required=True))
    return await _write(ctx, "POST", SMARTLISTS_PATH, body)


async def _smartlist_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _smartlist_path(args)
    body = _write_body(args, name=text_arg(args, "name"))
    current = await engine_call(ctx, "GET", path)
    if current.etag is None:
        raise ToolFailure("engine_error", f"GET {path} sent no ETag")
    return await _write(ctx, "PUT", path, body, if_match=current.etag)


_STRING = {"type": "string"}
_RULE = {
    "type": "object",
    "description": (
        "The rule AST, passed unchanged: a predicate {field, op, value} (e.g. "
        '{"field": "bpm", "op": ">=", "value": 120}) or {op: "and"|"or", children: [...]}.'
    ),
}
_ORDER_BY = {
    **_STRING,
    "description": "Sort key and direction, e.g. 'bpm asc'; the engine default is 'added_date desc'.",
}
_SMARTLIST_ID = {**_STRING, "description": "The smartlist id from smartlist_list."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="smartlist_list",
        description=(
            "Every smartlist with its live track count. Answers {items, total}; each item has id, "
            "name, rule, rule_summary, order_by, count. "
            "CLI: opendj api GET '/api/v1/smartlists?include_counts=true'; MCP: library with that path."
        ),
        parameters={"type": "object", "properties": {}},
        gate="read",
        run=_smartlist_list,
    ),
    ToolSpec(
        name="smartlist_tracks",
        description=(
            "The tracks a smartlist's rule matches now, in its order. Answers {smartlist_id, name, "
            "rule_summary, total, items}; each item has stable_id, title, artist, bpm, key, rating, "
            "genre, duration_ms. An unknown id fails engine_error naming 404. "
            "CLI: opendj api GET /api/v1/smartlists/{id}/tracks; MCP: library with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "smartlist_id": _SMARTLIST_ID,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TRACKS_LIMIT,
                    "default": DEFAULT_TRACKS_LIMIT,
                },
            },
            "required": ["smartlist_id"],
        },
        gate="read",
        run=_smartlist_tracks,
    ),
    ToolSpec(
        name="smartlist_create",
        description=(
            "Create a smartlist from a name and a rule. A rule the engine refuses fails "
            "invalid_arguments with the engine's reason; a name in use fails engine_error naming 409. "
            "Answers the new smartlist (id, name, rule, rule_summary, order_by) and its etag. "
            'CLI: opendj api POST /api/v1/smartlists --json \'{"name":...,"rule":...}\'; MCP: library.'
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "The new smartlist's name."},
                "rule": _RULE,
                "order_by": _ORDER_BY,
            },
            "required": ["name", "rule"],
        },
        gate="confirm",
        run=_smartlist_create,
    ),
    ToolSpec(
        name="smartlist_update",
        description=(
            "Replace a smartlist's whole rule (and optionally its name or order_by). Reads the "
            "current etag itself: no etag needed. A rule the engine refuses fails invalid_arguments "
            "with the engine's reason. Answers the updated smartlist and its new etag. "
            "CLI: opendj api PUT /api/v1/smartlists/{id} -H 'If-Match: \"<etag>\"' --json '{\"rule\":...}'; "
            "MCP: library with the If-Match header."
        ),
        parameters={
            "type": "object",
            "properties": {
                "smartlist_id": _SMARTLIST_ID,
                "rule": _RULE,
                "name": {**_STRING, "description": "A new name; omit to keep the current one."},
                "order_by": _ORDER_BY,
            },
            "required": ["smartlist_id", "rule"],
        },
        gate="confirm",
        run=_smartlist_update,
    ),
)
