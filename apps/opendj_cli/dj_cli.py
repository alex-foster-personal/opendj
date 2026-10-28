"""``opendj dj <tool>``: the AutoDJ tool surface on the command line (AGENT-29).

The CLI twin of :mod:`mcp_dj_tools`: every subcommand is one :class:`DjService`
call and prints the same JSON document the MCP tool returns, so an agent that
shells out and one that speaks MCP see identical answers. Output is always
JSON (the documents are nested; there is no useful text rendering).

Plans are read from a file path or ``-`` for stdin, so a proposal can be
piped straight back in::

    opendj dj propose <in_stable_id> --intent "keep the energy up" \\
        | jq '.proposals[0].plan' | opendj dj schedule -
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from apps.autodj.http import build_service
from apps.opendj_cli import (
    EXIT_CONFIRMED,
    EXIT_FAILED,
    EXIT_NO_ENGINE,
    EXIT_NO_PAGE,
    EXIT_UNCONFIRMED,
)
from apps.opendj_cli.dj_service import AMEND_ACTIONS, DjError, DjService
from apps.opendj_cli.origin import (
    EngineIdentityMismatch,
    EngineNotRunning,
    resolve_verified_origin,
)
from apps.opendj_cli.verbs import describe_verbs

#: DjError codes that mean the page, not the request, was the problem.
_NO_PAGE_CODES = frozenset({"no_performance_page"})
#: DjError codes for a well-formed request the live mix refused or never confirmed.
_REFUSED_CODES = frozenset(
    {
        "plan_invalid",
        "plan_refused",
        "plan_busy",
        "plan_unconfirmed",
        "amend_refused",
        "autoplay_on",
        "deck_busy",
        "order_failed",
        "load_unconfirmed",
    }
)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="opendj dj", description="AutoDJ tools: read the mix, plan and perform transitions.")
    tools = parser.add_subparsers(dest="tool", required=True, parser_class=_Parser)
    tools.add_parser("state", help="what is playing per deck: bar, section, mixer, running plan")
    profile = tools.add_parser("profile", help="one track's sections, energy, vocals, key segments")
    profile.add_argument("stable_id")
    windows = tools.add_parser("windows", help="ranked mix-out or mix-in windows for a track")
    windows.add_argument("stable_id")
    windows.add_argument("role", choices=("out", "in"))
    windows.add_argument("--limit", type=int, default=5)
    score = tools.add_parser("score", help="score mixing one track into another")
    score.add_argument("out_stable_id")
    score.add_argument("in_stable_id")
    score.add_argument("--out-bar", type=int)
    score.add_argument("--in-bar", type=int)
    score.add_argument("--overlap", type=int)
    suggest = tools.add_parser("suggest", help="ranked next tracks (default: for the master deck)")
    suggest.add_argument("stable_id", nargs="?")
    suggest.add_argument("--top", type=int, default=10)
    tools.add_parser("styles", help="the transition style catalog")
    tools.add_parser("verbs", help="every primitive the command tool accepts, with its arguments")
    propose = tools.add_parser("propose", help="complete, validated transition plans into a track")
    propose.add_argument("in_stable_id")
    propose.add_argument("--intent", help='free text, e.g. "wind down" or "keep vocals clean"')
    propose.add_argument("--out-deck", type=int)
    propose.add_argument("--in-deck", type=int)
    propose.add_argument("--top", type=int, default=3)
    for name, text in (
        ("validate", "check a plan against both tracks and the live mix"),
        ("schedule", "perform a plan on the page (continues a playing set only)"),
    ):
        command = tools.add_parser(name, help=text)
        command.add_argument("plan", help="path to a plan JSON file, or - for stdin")
    amend = tools.add_parser("amend", help="abort, finish_now or extend a scheduled plan")
    amend.add_argument("plan_id")
    amend.add_argument("action", choices=AMEND_ACTIONS)
    amend.add_argument("--bars", type=int)
    return parser


def _read_plan(source: str) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as error:
        raise _UsageError(f"cannot read plan {source}: {error}") from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise _UsageError(f"plan {source} is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise _UsageError(f"plan {source} must be a JSON object")
    return document


def _call(args: argparse.Namespace) -> Callable[[DjService], dict[str, Any]]:
    tool = args.tool
    if tool == "state":
        return lambda service: service.mix_state()
    if tool == "profile":
        return lambda service: service.track_profile(args.stable_id)
    if tool == "windows":
        return lambda service: service.mix_windows(args.stable_id, args.role, args.limit)
    if tool == "score":
        return lambda service: service.score_pair(
            args.out_stable_id, args.in_stable_id, args.out_bar, args.in_bar, args.overlap
        )
    if tool == "suggest":
        return lambda service: service.suggest_next(args.stable_id, None, args.top)
    if tool == "styles":
        return lambda service: service.styles()
    if tool == "propose":
        return lambda service: service.propose(
            args.in_stable_id, out_deck=args.out_deck, in_deck=args.in_deck, intent=args.intent, top=args.top
        )
    if tool in ("validate", "schedule"):
        plan = _read_plan(args.plan)
        if tool == "validate":
            return lambda service: service.validate(plan)
        return lambda service: service.schedule(plan)
    return lambda service: service.amend(args.plan_id, args.action, args.bars)


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def _exit_for(code: str) -> int:
    if code in _NO_PAGE_CODES:
        return EXIT_NO_PAGE
    if code in _REFUSED_CODES:
        return EXIT_UNCONFIRMED
    return EXIT_FAILED


def run(
    rest: Sequence[str],
    *,
    lock: Path | None,
    service_factory: Callable[[Path | None], DjService] | None = None,
) -> int:
    """Run one ``opendj dj`` tool; ``service_factory`` is the tests' seam."""
    try:
        args = _parser().parse_args(list(rest))
        call = _call(args)
    except _UsageError as error:
        _emit({"error": "usage", "message": str(error)})
        return EXIT_FAILED
    if args.tool == "styles":  # the catalog is static: no engine needed
        _emit({"styles": [s.model_dump(mode="json") for s in build_service().list_styles()]})
        return EXIT_CONFIRMED
    if args.tool == "verbs":  # the same document as the MCP list_verbs tool, also static
        _emit({"verbs": describe_verbs()})
        return EXIT_CONFIRMED
    try:
        service = (
            service_factory(lock) if service_factory is not None else DjService(origin=resolve_verified_origin(lock))
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
        return _exit_for(error.code)
    _emit(document)
    return EXIT_CONFIRMED
