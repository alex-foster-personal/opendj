"""Fix a track whose file went missing: ``relocate_candidates`` and ``relocate_apply``.

``relocate_candidates`` reads the engine's ranked filesystem matches for one
track's dead recorded path (``GET /api/v1/relocate/candidates/{id}``) and
hands the model only path, confidence, signals and triple_validated per
candidate: the identity token is for this module, not the model.

``relocate_apply`` adopts one of those paths (``POST /api/v1/relocate/{id}/apply``).
The model names only ``stable_id`` and ``new_path``. The tool reads the track's
ETag and the candidate list itself, refuses a ``new_path`` that is not listed
(``invalid_arguments``, nothing sent), and fills ``expected_original_path``,
``expected_vendor_id`` and ``expected_candidate_identity`` from that list, so a
stale or invented target cannot be aimed. It never sets
``allow_different_recording``: a length mismatch stays the engine's refusal.
The route itself still answers to the rekordbox writeback gate.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
RELOCATE_PATH: str = "/api/v1/relocate"
TRACKS_PATH: str = "/api/v1/tracks"

#: What one candidate carries for the model.
CANDIDATE_FIELDS: tuple[str, ...] = ("path", "confidence", "signals", "triple_validated")
DEFAULT_LIMIT: int = 5
#: The candidates route's own bound (``limit`` le=20); apply reads this many.
MAX_LIMIT: int = 20


# ----- helpers ------------------------------------------------------------
def _quoted(stable_id: str) -> str:
    return quote(stable_id, safe="")


async def _candidate_list(ctx: ToolContext, stable_id: str, limit: int) -> dict[str, Any]:
    query = urlencode({"limit": limit})
    return await engine_json(ctx, "GET", f"{RELOCATE_PATH}/candidates/{_quoted(stable_id)}?{query}")


def _listed_candidate(listing: dict[str, Any], new_path: str) -> dict[str, Any]:
    """The listed candidate at ``new_path``; invalid_arguments naming the listed paths otherwise."""
    for candidate in listing["candidates"]:
        if candidate["path"] == new_path:
            return candidate
    listed = [candidate["path"] for candidate in listing["candidates"]]
    raise ToolFailure(
        "invalid_arguments",
        f"new_path {new_path!r} is not a relocate candidate for {listing['stable_id']}; listed: {listed}",
    )


# ----- tools --------------------------------------------------------------
async def _relocate_candidates(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = str(text_arg(args, "stable_id", required=True))
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT) or DEFAULT_LIMIT
    listing = await _candidate_list(ctx, stable_id, limit)
    return {
        "stable_id": listing["stable_id"],
        "original_path": listing["original_path"],
        "candidates": [{name: row[name] for name in CANDIDATE_FIELDS} for row in listing["candidates"]],
    }


async def _relocate_apply(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = str(text_arg(args, "stable_id", required=True))
    new_path = str(text_arg(args, "new_path", required=True))
    track = await engine_call(ctx, "GET", f"{TRACKS_PATH}/{_quoted(stable_id)}")
    if track.etag is None:
        raise ToolFailure("engine_error", f"GET {TRACKS_PATH}/{stable_id} sent no ETag to send as If-Match")
    listing = await _candidate_list(ctx, stable_id, MAX_LIMIT)
    candidate = _listed_candidate(listing, new_path)
    body = {
        "new_path": candidate["path"],
        "expected_candidate_identity": candidate["identity_token"],
        "expected_original_path": listing["original_path"],
        "expected_vendor_id": listing["vendor_id"],
        "confirm": True,
    }
    answer = await engine_call(ctx, "POST", f"{RELOCATE_PATH}/{_quoted(stable_id)}/apply", body, if_match=track.etag)
    return answer.body


_STABLE_ID = {"type": "string", "description": "The track's stable_id."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="relocate_candidates",
        description=(
            "Files on disk that may be a track's missing file, ranked best first. Answers "
            "{stable_id, original_path, candidates}; each candidate has path, confidence, signals "
            "and triple_validated. original_path null means the track has no local file to fix. "
            "Pass a path to relocate_apply. CLI: opendj api GET /api/v1/relocate/candidates/{stable_id}; "
            "MCP: none (MCP blocks /relocate/ as a rekordbox writeback surface)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": _STABLE_ID,
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
            "required": ["stable_id"],
        },
        gate="read",
        run=_relocate_candidates,
    ),
    ToolSpec(
        name="relocate_apply",
        description=(
            "Point a track at one of its relocate_candidates paths. new_path must be listed by "
            "relocate_candidates right now, or the call fails invalid_arguments and nothing is "
            "written. A candidate of a different length is refused by the engine. Writes rekordbox's "
            "FolderPath when the track is mapped there, else the library's file_path. "
            "CLI: opendj api POST /api/v1/relocate/{stable_id}/apply with If-Match; MCP: none."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": _STABLE_ID,
                "new_path": {"type": "string", "description": "A path relocate_candidates listed, exactly."},
            },
            "required": ["stable_id", "new_path"],
        },
        gate="confirm",
        run=_relocate_apply,
    ),
)
