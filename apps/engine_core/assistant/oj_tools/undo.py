"""OJ's ``undo_last``: step back OJ's own newest library-organization edit.

``POST /api/v1/undo`` reverts the newest live entry on the caller's undo
session. Every ``engine_call`` write sends ``X-Undo-Session: OJ_UNDO_SESSION``
(``core_tools.py``), so this tool undoes only what OJ itself changed (a tag,
a tag type, a section, a work), never an edit the user made by hand in a web UI
window or the CLI, which sit on their own sessions. The engine refuses with
404 when OJ's session has nothing left to undo and 409 when the row changed
since; both reach the model as ``engine_error`` and nothing is written.
Playlist edits keep their own history (``POST /api/v1/playlist-history/undo``).
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json

# ----- configuration ------------------------------------------------------
UNDO_PATH: str = "/api/v1/undo"


# ----- tools --------------------------------------------------------------
async def _undo_last(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    answer = await engine_json(ctx, "POST", UNDO_PATH)
    return {"undo_id": answer["undo_id"], "op": answer["op"]}


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="undo_last",
        description=(
            "Undo OJ's own newest library-organization edit (tags, tag types, sections, works), "
            "never one the user made by hand. Answers {undo_id, op}, op naming what was undone "
            "(e.g. tag.create). Nothing left to undo fails engine_error naming 404; a row changed "
            "since fails naming 409; nothing is written on failure. Playlist edits are not covered. "
            "CLI: opendj work --undo-session <id> undo; MCP: library with POST /api/v1/undo."
        ),
        parameters={"type": "object", "properties": {}},
        gate="confirm",
        run=_undo_last,
    ),
)
