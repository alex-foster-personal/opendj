"""``opendj dj pair-transition``: transition memory on the command line (MEMORY-02).

    opendj dj pair-transition get --out X --in Y [--out-deck 2 --in-deck 1]
    opendj dj pair-transition set PLAN.json --source edited [--style NAME]
    opendj dj pair-transition clear --out X --in Y
    opendj dj pair-transition played --out X --in Y [PLAN.json --source planner --style NAME]
    opendj dj pair-transition capture

Output is always the JSON document ``/api/v1/autodj/pair-transition`` answers, so the CLI and the routes read one
shape. ``set`` and ``played`` take a plan path or ``-`` for stdin; ``--style`` defaults to the plan's own.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlencode

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE, EXIT_UNCONFIRMED
from apps.opendj_cli.dj_plan_io import PlanIoError, read_plan_json
from apps.opendj_cli.dj_service import DjError
from apps.opendj_cli.origin import (
    EngineIdentityMismatch,
    EngineNotRunning,
    EngineOrigin,
    resolve_verified_origin,
    unreachable,
)

PAIR_PATH = "/api/v1/autodj/pair-transition"
TIMEOUT_S = 10.0
#: A well-formed request the store refused, exit 4 like the other dj refusals.
_REFUSED_CODES = frozenset({"plan_invalid", "pair_transition_not_found"})
_SOURCES = ("planner", "captured", "edited")


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="opendj dj pair-transition", description="the saved transition for an ordered pair of tracks")
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    for name, text in (("get", "read a pair's saved plan and meta"), ("clear", "forget a pair's saved plan")):
        action = actions.add_parser(name, help=text)
        action.add_argument("--out", dest="out_id", required=True, help="outgoing track stable_id")
        action.add_argument("--in", dest="in_id", required=True, help="incoming track stable_id")
        if name == "get":
            action.add_argument("--out-deck", type=int, help="move the plan onto this outgoing deck (with --in-deck)")
            action.add_argument("--in-deck", type=int, help="move the plan onto this incoming deck (with --out-deck)")
    save = actions.add_parser("set", help="make a plan the pair's default")
    save.add_argument("plan", help="path to a plan JSON file, or - for stdin")
    save.add_argument("--source", required=True, choices=_SOURCES)
    save.add_argument("--style", help="style name; default the plan's own")
    played = actions.add_parser("played", help="record that a pair's transition ran, saving the plan given")
    played.add_argument("plan", nargs="?", help="the plan that ran: a path, or - for stdin; omit if the saved one ran")
    played.add_argument("--out", dest="out_id", required=True, help="outgoing track stable_id")
    played.add_argument("--in", dest="in_id", required=True, help="incoming track stable_id")
    played.add_argument("--source", choices=_SOURCES, help="required with a plan")
    played.add_argument("--style", help="style name; default the plan's own")
    actions.add_parser("capture", help="what the hand-mix capture is doing, and how it last ended")
    return parser


@dataclass
class DjPairService:
    origin: EngineOrigin
    #: Tests hand an ASGI transport here.
    transport: httpx.BaseTransport | None = None

    def send(self, method: str, path: str, *, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.origin.base_url}{path}"
        try:
            with httpx.Client(timeout=TIMEOUT_S, trust_env=False, transport=self.transport) as client:
                response = client.request(method, url, json=None if body is None else dict(body))
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error
        if response.is_success:
            return {"cleared": True} if response.status_code == 204 else response.json()
        try:
            detail = response.json()
        except ValueError:
            detail = {}
        code = detail.get("error") if isinstance(detail, dict) else None
        raise DjError(
            code if isinstance(code, str) else "upstream_error",
            f"{method} {path} -> {response.status_code}: {response.text[:300]}",
            path=path,
            status_code=response.status_code,
        )


def _query(args: argparse.Namespace) -> str:
    decks = {name: getattr(args, name, None) for name in ("out_deck", "in_deck")}
    pairs = {"out": args.out_id, "in": args.in_id} | {name: deck for name, deck in decks.items() if deck is not None}
    return f"{PAIR_PATH}?{urlencode(pairs)}"


def _save_body(plan: dict[str, Any], source: str | None, style: str | None) -> dict[str, Any]:
    if source is None:
        raise _UsageError("--source is required with a plan")
    return {"plan": plan, "source": source, "style": style if style is not None else plan.get("style")}


def _call(rest: Sequence[str]) -> Callable[[DjPairService], dict[str, Any]]:
    args = _parser().parse_args(list(rest))
    if args.action == "get":
        return lambda service: service.send("GET", _query(args))
    if args.action == "clear":
        return lambda service: service.send("DELETE", _query(args))
    if args.action == "capture":
        return lambda service: service.send("GET", f"{PAIR_PATH}/capture")
    if args.action == "set":
        body = _save_body(read_plan_json(args.plan), args.source, args.style)
        return lambda service: service.send("PUT", PAIR_PATH, body=body)
    pair = {"out": args.out_id, "in": args.in_id}
    played = pair if args.plan is None else {**pair, **_save_body(read_plan_json(args.plan), args.source, args.style)}
    return lambda service: service.send("POST", f"{PAIR_PATH}/played", body=played)


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def run(
    rest: Sequence[str],
    *,
    lock: Path | None,
    service_factory: Callable[[Path | None], DjPairService] | None = None,
) -> int:
    """Run one ``opendj dj pair-transition`` call; ``service_factory`` is the tests' seam."""
    try:
        call = _call(rest)
    except (_UsageError, PlanIoError) as error:
        _emit({"error": "usage", "message": str(error)})
        return EXIT_FAILED
    try:
        service = (
            service_factory(lock)
            if service_factory is not None
            else DjPairService(origin=resolve_verified_origin(lock))
        )
        document = call(service)
    except EngineIdentityMismatch as error:
        _emit({"error": "engine_identity_mismatch", "message": str(error)})
        return EXIT_NO_ENGINE
    except EngineNotRunning as error:
        _emit({"error": "engine_not_running", "message": str(error)})
        return EXIT_NO_ENGINE
    except DjError as error:
        _emit(error.document())
        return EXIT_UNCONFIRMED if error.code in _REFUSED_CODES else EXIT_FAILED
    _emit(document)
    return EXIT_CONFIRMED
