"""``opendj history``: play History by night, the agent twin of the History view (PLAYS, issue #9745).

    opendj history list [tz=ZONE]
    opendj history show <YYYY-MM-DD> [tz=ZONE]
    opendj history save-playlist <YYYY-MM-DD> [name=NAME] [tz=ZONE]
    opendj history delete-play <stable_id> <play_id>
    opendj history delete-day <YYYY-MM-DD> [IANA_ZONE]

``list`` reads ``GET /api/v1/history`` (days with play counts, newest first), ``show`` reads
``GET /api/v1/history/{date}`` (that night's plays in order) and ``save-playlist`` posts
``POST /api/v1/history/{date}/playlist``. A day runs from local 06:00 to 06:00 the next day.
Options are ``key=value`` tokens like the bus verbs (the global parser refuses ``--flags`` after a
standalone command). The delete verbs live in ``track_cli`` (PLAYS-10523) and pass through unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

from apps.opendj_cli import EXIT_FAILED, api_cli, track_cli
from apps.opendj_cli.refusal import fail as _fail

USAGE = (
    "usage: opendj history list [tz=ZONE]\n"
    "       opendj history show <YYYY-MM-DD> [tz=ZONE]\n"
    "       opendj history save-playlist <YYYY-MM-DD> [name=NAME] [tz=ZONE]\n"
    "       opendj history delete-play <stable_id> <play_id>\n"
    "       opendj history delete-day <YYYY-MM-DD> [IANA_ZONE]"
)
HISTORY_PATH = "/api/v1/history"
READ_VERBS = ("list", "show", "save-playlist")


@dataclass(frozen=True)
class HistoryArgs:
    """Parsed options of one read or save verb."""

    day: date | None
    tz: str | None
    name: str | None


def _parse(verb: str, args: Sequence[str]) -> HistoryArgs | None:
    allowed = {"tz", "name"} if verb == "save-playlist" else {"tz"}
    options: dict[str, str] = {}
    positional: list[str] = []
    for token in args:
        key, separator, value = token.partition("=")
        if separator and key in allowed and value and key not in options:
            options[key] = value
        elif separator:
            return None
        else:
            positional.append(token)
    if len(positional) != (0 if verb == "list" else 1):
        return None
    try:
        day = date.fromisoformat(positional[0]) if positional else None
    except ValueError:
        return None
    return HistoryArgs(day=day, tz=options.get("tz"), name=options.get("name"))


def _with_tz(path: str, tz: str | None) -> str:
    return path if tz is None else f"{path}?tz={quote(tz, safe='')}"


def history_request(verb: str, args: Sequence[str]) -> list[str] | None:
    """The ``api_cli`` argv for one read or save verb; None when the arguments do not parse."""
    opts = _parse(verb, args)
    if opts is None:
        return None
    if opts.day is None:
        return ["GET", _with_tz(HISTORY_PATH, opts.tz)]
    day_path = f"{HISTORY_PATH}/{opts.day.isoformat()}"
    if verb == "show":
        return ["GET", _with_tz(day_path, opts.tz)]
    body = {} if opts.name is None else {"name": opts.name}
    return ["POST", _with_tz(f"{day_path}/playlist", opts.tz), "--json", json.dumps(body)]


def run(
    rest: Sequence[str],
    *,
    as_json: bool,
    state_db: Path | None = None,
) -> int:
    """Dispatch one ``opendj history`` verb: read and save here, deletes through ``track_cli``."""
    if not rest or rest[0] not in READ_VERBS:
        return track_cli.run_history(rest, as_json=as_json, state_db=state_db)
    verb, *args = rest
    request = history_request(verb, args)
    if request is None:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    return api_cli.run(request, as_json=as_json)


__all__ = ["HISTORY_PATH", "READ_VERBS", "USAGE", "history_request", "run"]
