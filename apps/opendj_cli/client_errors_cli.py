"""``opendj client_errors``: filtered browser errors (AGENT-8304, MCP ``client_errors`` parity).

    opendj [--json] client_errors [--since ISO] [--build-sha PREFIX] [--kind KIND]
                                  [--contains TEXT] [--limit N] [--untriaged]

``--json`` prints the document :func:`fetch_client_errors` returns, identical
to the MCP tool's result. Text mode prints one line per error.

-Claude
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.client_errors_query import (
    DEFAULT_LIMIT,
    ClientErrorFilters,
    ClientErrorsRequestFailed,
    fetch_client_errors,
)
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

USAGE = (
    "usage: opendj client_errors [--since ISO] [--build-sha PREFIX] [--kind KIND] "
    "[--contains TEXT] [--limit N] [--untriaged]"
)
_MESSAGE_WIDTH = 160


class _ArgvParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{message}; {USAGE}")


def _parser() -> _ArgvParser:
    parser = _ArgvParser(prog="opendj client_errors", add_help=False)
    parser.add_argument("--since")
    parser.add_argument("--build-sha", dest="build_sha")
    parser.add_argument("--kind")
    parser.add_argument("--contains")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--untriaged", action="store_true")
    return parser


def parse_filters(rest: Sequence[str]) -> ClientErrorFilters:
    return ClientErrorFilters(**vars(_parser().parse_args(list(rest))))


def _text_line(row: dict[str, Any]) -> str:
    message = str(row["message"]).splitlines()[0] if row["message"] else ""
    if len(message) > _MESSAGE_WIDTH:
        message = message[: _MESSAGE_WIDTH - 3] + "..."
    sha = (row["build_sha"] or "-")[:10]
    return f"{row['timestamp']}  {sha}  {row['kind']}  {row['event_id']}  {message}"


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    try:
        filters = parse_filters(rest)
        target = resolve_backend_base_url(lock_path=lock)
        document = fetch_client_errors(target.base_url, filters)
    except UsageError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    except EngineNotRunning as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except (ClientErrorsRequestFailed, httpx.HTTPError) as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
    elif document["errors"]:
        print("\n".join(_text_line(row) for row in document["errors"]))
    else:
        print("no client errors match")
    return EXIT_CONFIRMED
