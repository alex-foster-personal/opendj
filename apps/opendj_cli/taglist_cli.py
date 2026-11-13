"""``opendj taglist``: the Tag List prep list without the UI (rekordbox Tag List parity, issue #10028).

    opendj taglist list
    opendj taglist add <stable_id> [<stable_id> ...]
    opendj taglist remove <stable_id>
    opendj taglist clear
    opendj taglist save <playlist name>

Each verb is one call to the ``/api/v1/taglist`` routes through ``api_cli``, so refusals
(the 100-track cap, an unknown track, an empty list on save) come back exactly as the
route answers them. ``save`` joins the remaining words into the playlist name.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from apps.opendj_cli import EXIT_FAILED, api_cli
from apps.opendj_cli.refusal import fail as _fail

USAGE = (
    "usage: opendj taglist list\n"
    "       opendj taglist add <stable_id> [<stable_id> ...]\n"
    "       opendj taglist remove <stable_id>\n"
    "       opendj taglist clear\n"
    "       opendj taglist save <playlist name>"
)
TAGLIST_PATH = "/api/v1/taglist"


def taglist_request(verb: str, args: Sequence[str]) -> list[str] | None:
    """The ``api_cli`` argv for one verb; None when the verb or its arguments are wrong."""
    if verb == "list" and not args:
        return ["GET", TAGLIST_PATH]
    if verb == "add" and args:
        return ["POST", f"{TAGLIST_PATH}/items", "--json", json.dumps({"stable_ids": list(args)})]
    if verb == "remove" and len(args) == 1:
        return ["DELETE", f"{TAGLIST_PATH}/items/{quote(args[0], safe='')}"]
    if verb == "clear" and not args:
        return ["DELETE", TAGLIST_PATH]
    if verb == "save" and args:
        return ["POST", f"{TAGLIST_PATH}:save", "--json", json.dumps({"name": " ".join(args)})]
    return None


def run(rest: Sequence[str], *, as_json: bool, lock: Path | None = None) -> int:
    """Dispatch one ``opendj taglist`` verb to its route."""
    request = taglist_request(rest[0], rest[1:]) if rest else None
    if request is None:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    return api_cli.run(request, as_json=as_json, lock=lock)


__all__ = ["TAGLIST_PATH", "USAGE", "run", "taglist_request"]
