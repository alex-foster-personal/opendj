"""Review duplicates: ``dedup_clusters``, ``dedup_decide``, ``dedup_apply`` and ``dedup_undo``.

Every tool calls the route the duplicate review UI calls (``/api/v1/dedup``),
so OJ, the CLI (``opendj api``) and MCP (``app_state``, ``library``) drive one
write path. ``dedup_clusters`` is gate ``read``; the three writes are gate
``confirm``.

Each write reads ``GET /api/v1/dedup/clusters`` itself first: the answer's
``revision`` is the decision-store ETag the write sends as ``If-Match``, and the
cluster row gives the ``cluster_key`` (a digest of the members) the route
checks. A stale revision or a changed cluster is the engine's 409, reported as
``engine_error`` naming 409 and never retried. A ``cluster_id`` the list does
not have fails ``engine_error`` naming 404 without a write.

An apply only rewrites OpenDJ playlist memberships (aliases become the
survivor); it never touches an audio file or rekordbox. ``confirm_cue_loss``
is required with no default: ``false`` lets the engine refuse (409
``DEDUP_CUE_LOSS_REQUIRES_CONFIRMATION``) when an alias holds cues the survivor
lacks, ``true`` drops them. There is no scan tool: a scan fingerprints the
whole library, and the UI starts it.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import EngineAnswer, engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
CLUSTERS_PATH: str = "/api/v1/dedup/clusters"
#: The decision route's ``ActionLiteral``.
ACTIONS: tuple[str, ...] = ("merge", "keep-all", "skip")
#: What one cluster member carries for the model; ``cue_count`` shows what an apply could drop.
MEMBER_FIELDS: tuple[str, ...] = ("stable_id", "title", "artist", "cue_count")
DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 100
#: SQLite rowid ceiling for a cluster id.
MAX_CLUSTER_ID: int = 2**63 - 1


# ----- helpers ------------------------------------------------------------
def _cluster_id(args: dict[str, Any]) -> int:
    cluster_id = int_arg(args, "cluster_id", low=1, high=MAX_CLUSTER_ID)
    if cluster_id is None:
        raise ToolFailure("invalid_arguments", "cluster_id is required (from dedup_clusters)")
    return cluster_id


def _confirm_cue_loss(args: dict[str, Any]) -> bool:
    value = args.get("confirm_cue_loss")
    if not isinstance(value, bool):
        raise ToolFailure("invalid_arguments", f"confirm_cue_loss must be true or false, got {value!r}")
    return value


def _compact(cluster: dict[str, Any]) -> dict[str, Any]:
    decision = cluster["decision"]
    return {
        "cluster_id": cluster["cluster_id"],
        "cluster_key": cluster["cluster_key"],
        "survivor_stable_id": cluster["survivor_stable_id"],
        "flagged_manual_review": cluster["flagged_manual_review"],
        "members": [{name: member[name] for name in MEMBER_FIELDS} for member in cluster["members"]],
        "decision": (
            None if decision is None else {name: decision[name] for name in ("action", "survivor", "pending_apply")}
        ),
    }


async def _cluster_and_etag(ctx: ToolContext, cluster_id: int) -> tuple[dict[str, Any], str]:
    """The cluster row as the engine lists it now, and the decision-store ETag to send as If-Match."""
    page = await engine_json(ctx, "GET", CLUSTERS_PATH)
    for cluster in page["clusters"]:
        if cluster["cluster_id"] == cluster_id:
            return cluster, page["revision"]
    known = ", ".join(str(cluster["cluster_id"]) for cluster in page["clusters"][:20]) or "none"
    raise ToolFailure("engine_error", f"GET {CLUSTERS_PATH} -> 404: no cluster_id {cluster_id} (listed: {known})")


async def _write(ctx: ToolContext, cluster_id: int, verb: str, body: dict[str, Any], etag: str) -> EngineAnswer:
    return await engine_call(ctx, "POST", f"{CLUSTERS_PATH}/{cluster_id}/{verb}", body, if_match=etag)


# ----- tools --------------------------------------------------------------
async def _dedup_clusters(ctx: ToolContext, args: dict[str, Any]) -> Any:
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT) or DEFAULT_LIMIT
    offset = int_arg(args, "offset", low=0, high=MAX_CLUSTER_ID) or 0
    page = await engine_json(ctx, "GET", CLUSTERS_PATH)
    clusters = page["clusters"]
    return {
        "etag": page["revision"],
        "note": page.get("note"),
        "total": len(clusters),
        "clusters": [_compact(cluster) for cluster in clusters[offset : offset + limit]],
        "more": offset + limit < len(clusters),
    }


async def _dedup_decide(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cluster_id = _cluster_id(args)
    action = text_arg(args, "action", required=True)
    if action not in ACTIONS:
        raise ToolFailure("invalid_arguments", f"action must be one of {', '.join(ACTIONS)}, got {action!r}")
    survivor = text_arg(args, "survivor")
    cluster, etag = await _cluster_and_etag(ctx, cluster_id)
    body = {
        "cluster_key": cluster["cluster_key"],
        "survivor": survivor or cluster["survivor_stable_id"],
        "action": action,
    }
    answer = await _write(ctx, cluster_id, "decision", body, etag)
    return {"cluster_id": cluster_id, "action": action, "survivor": body["survivor"], "etag": answer.etag}


async def _dedup_apply(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cluster_id = _cluster_id(args)
    survivor = text_arg(args, "survivor", required=True)
    confirm_cue_loss = _confirm_cue_loss(args)
    cluster, etag = await _cluster_and_etag(ctx, cluster_id)
    body = {"cluster_key": cluster["cluster_key"], "survivor": survivor, "confirm_cue_loss": confirm_cue_loss}
    answer = await _write(ctx, cluster_id, "apply", body, etag)
    if "playlists" not in answer.body:
        # The answer passed the result cap (long before/after lists); the write itself succeeded.
        return {"cluster_id": cluster_id, "survivor": survivor, "etag": answer.etag, "playlists": answer.body}
    rewritten = [{"playlist_id": row["playlist_id"], "name": row["name"]} for row in answer.body["playlists"]]
    return {"cluster_id": cluster_id, "survivor": survivor, "etag": answer.etag, "playlists": rewritten}


async def _dedup_undo(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cluster_id = _cluster_id(args)
    cluster, etag = await _cluster_and_etag(ctx, cluster_id)
    decision = cluster["decision"]
    survivor = decision["survivor"] if decision is not None else cluster["survivor_stable_id"]
    body = {"cluster_key": cluster["cluster_key"], "survivor": survivor}
    answer = await _write(ctx, cluster_id, "undo", body, etag)
    return {"cluster_id": cluster_id, "playlist_ids": answer.body["playlist_ids"], "etag": answer.etag}


_CLUSTER_ID = {"type": "integer", "minimum": 1, "description": "The cluster_id from dedup_clusters."}
_STABLE_ID = {"type": "string", "description": "A member's stable_id from dedup_clusters."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="dedup_clusters",
        description=(
            "List the duplicate clusters the last scan found. Answers {etag, note, total, clusters, more}; "
            "each cluster has cluster_id, cluster_key, survivor_stable_id (the suggested keeper), "
            "flagged_manual_review, members (stable_id, title, artist, cue_count) and decision "
            "(action, survivor, pending_apply, or null). An empty list with a note means no scan has run. "
            "CLI: opendj api GET /api/v1/dedup/clusters; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
        gate="read",
        run=_dedup_clusters,
    ),
    ToolSpec(
        name="dedup_decide",
        description=(
            "Record a review decision for one cluster without applying it: merge, keep-all or skip, "
            "with survivor defaulting to the suggested one. Reads the cluster's If-Match itself. "
            "CLI: opendj api POST /api/v1/dedup/clusters/{id}/decision with If-Match; MCP: library POST."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cluster_id": _CLUSTER_ID,
                "action": {"type": "string", "enum": list(ACTIONS)},
                "survivor": _STABLE_ID,
            },
            "required": ["cluster_id", "action"],
        },
        gate="confirm",
        run=_dedup_decide,
    ),
    ToolSpec(
        name="dedup_apply",
        description=(
            "Merge one cluster: every OpenDJ playlist entry of an alias becomes the survivor. No audio "
            "file or rekordbox data is touched; dedup_undo reverses it. confirm_cue_loss is required: "
            "with false the engine refuses (engine_error naming 409 and DEDUP_CUE_LOSS_REQUIRES_CONFIRMATION) "
            "when an alias has cues the survivor lacks; ask the user before retrying with true. "
            "Answers the rewritten playlists. "
            "CLI: opendj api POST /api/v1/dedup/clusters/{id}/apply with If-Match; MCP: library POST."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cluster_id": _CLUSTER_ID,
                "survivor": _STABLE_ID,
                "confirm_cue_loss": {"type": "boolean", "description": "True only once the user accepts losing cues."},
            },
            "required": ["cluster_id", "survivor", "confirm_cue_loss"],
        },
        gate="confirm",
        run=_dedup_apply,
    ),
    ToolSpec(
        name="dedup_undo",
        description=(
            "Undo a cluster's applied merge: playlists go back to their entries before dedup_apply, and "
            "the merge decision stays pending. A cluster never applied fails engine_error naming 404. "
            "CLI: opendj api POST /api/v1/dedup/clusters/{id}/undo with If-Match; MCP: library POST."
        ),
        parameters={
            "type": "object",
            "properties": {"cluster_id": _CLUSTER_ID},
            "required": ["cluster_id"],
        },
        gate="confirm",
        run=_dedup_undo,
    ),
)
