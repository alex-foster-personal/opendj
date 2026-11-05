"""``opendj dj <tool>``: the AutoDJ tool surface on the command line (AGENT-29).

The CLI twin of :mod:`mcp_dj_tools`: every subcommand is one :class:`DjService`
call and prints the same JSON document the MCP tool returns, so an agent that
shells out and one that speaks MCP see identical answers. Output is always
JSON (the documents are nested; there is no useful text rendering).

Plans are read from a file path or ``-`` for stdin, as JSON or as a mix score
(AGENT-5858, :mod:`apps.autodj.notation`), so a proposal can be piped straight
back in, or saved, edited and scheduled::

    opendj dj propose <in_stable_id> --intent "keep the energy up" \\
        | jq -r '.proposals[0].notation' > blend.mix
    opendj dj show blend.mix --as grid
    opendj dj schedule blend.mix

A whole set (AGENT-27, :mod:`apps.autodj.setscore`) is planned, read and
performed one due transition at a time::

    opendj dj set-plan <id> <id> <id> --intent "wind down" | jq -r .score > set.mix
    opendj dj set-show set.mix --as grid
    opendj dj set-next set.mix      # again after each transition ends

``show``, ``set-show`` and ``styles`` work offline; every other tool needs the engine.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from apps.autodj.http import build_service
from apps.autodj.notation import ScoreError, looks_like_score, print_score, render_grid, score_document
from apps.autodj.plan import PlanError, parse_plan
from apps.autodj.setscore import SetPlan, print_set_score, read_set
from apps.opendj_cli import (
    EXIT_CONFIRMED,
    EXIT_FAILED,
    EXIT_NO_ENGINE,
    EXIT_NO_PAGE,
    EXIT_UNCONFIRMED,
    dj_pair_cli,
    dj_try_cli,
    dj_why_cli,
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
        "autoplay_handoff",
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
    show = tools.add_parser("show", help="print a plan as a mix score, bar grid or JSON (offline)")
    show.add_argument("plan", help="path to a plan (JSON or mix score), or - for stdin")
    show.add_argument("--as", dest="view", choices=("score", "grid", "json"), default="score")
    set_plan = tools.add_parser("set-plan", help="plan a whole set: a transition for each neighbor pair")
    set_plan.add_argument("stable_ids", nargs="+", help="the running order")
    set_plan.add_argument("--intent", help="free text, as for propose")
    set_plan.add_argument("--first-deck", type=int)
    set_plan.add_argument("--name")
    set_next = tools.add_parser("set-next", help="perform the set's transition that is due now")
    set_next.add_argument("set", help="path to a set (JSON or set score), or - for stdin")
    set_show = tools.add_parser("set-show", help="print a set as a set score, bar grids or JSON (offline)")
    set_show.add_argument("set", help="path to a set (JSON or set score), or - for stdin")
    set_show.add_argument("--as", dest="view", choices=("score", "grid", "json"), default="score")
    tools.add_parser("try", help="render and rank candidate transitions; see opendj dj try --help", add_help=False)
    tools.add_parser(
        "pair-transition",
        help="the saved transition for two tracks; see opendj dj pair-transition --help",
        add_help=False,
    )
    tools.add_parser("why", help="why AutoPlay chose its last track; see opendj dj why --help", add_help=False)
    tools.add_parser(
        "verdict", help="good, meh or bad on the transition that ran; see opendj dj verdict --help", add_help=False
    )
    tools.add_parser("autoplay-decision", help="AutoPlay's next decision: its window, track and plan")
    tools.add_parser("autoplay-decide", help="have AutoPlay decide now rather than at its target")
    amend = tools.add_parser("amend", help="abort, finish_now or extend a scheduled plan")
    amend.add_argument("plan_id")
    amend.add_argument("action", choices=AMEND_ACTIONS)
    amend.add_argument("--bars", type=int)
    return parser


def _read_text(source: str, what: str) -> str:
    try:
        return sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as error:
        raise _UsageError(f"cannot read {what} {source}: {error}") from error


def _read_set(source: str) -> SetPlan:
    try:
        return read_set(_read_text(source, "set"))
    except PlanError as error:
        raise _UsageError(f"set {source} is not a valid set: {error}") from error


def _read_plan(source: str) -> dict[str, Any]:
    text = _read_text(source, "plan")
    if looks_like_score(text):
        try:
            return score_document(text)
        except ScoreError as error:
            raise _UsageError(f"plan {source} is not a mix score: {error}") from error
    try:
        document = json.loads(text)
    except RecursionError as error:  # deep nesting exhausts the parser's stack: a plan that cannot be read
        raise DjError("plan_invalid", f"plan {source} is nested too deeply to read") from error
    except ValueError as error:
        raise _UsageError(f"plan {source} is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise _UsageError(f"plan {source} must be a JSON object")
    return document


def _set_call(args: argparse.Namespace) -> Callable[[DjService], dict[str, Any]]:
    if args.tool == "set-plan":
        return lambda service: service.plan_set(
            args.stable_ids, intent=args.intent, first_deck=args.first_deck, name=args.name
        )
    planned = _read_set(args.set)
    return lambda service: service.schedule_set_next(planned)


_NO_ARGUMENT_CALLS: dict[str, Callable[[DjService], dict[str, Any]]] = {
    "state": DjService.mix_state,
    "styles": DjService.styles,
    "autoplay-decision": DjService.autoplay_decision,
    "autoplay-decide": DjService.autoplay_decide_now,
}


def _call(args: argparse.Namespace) -> Callable[[DjService], dict[str, Any]]:
    tool = args.tool
    if tool in _NO_ARGUMENT_CALLS:
        return _NO_ARGUMENT_CALLS[tool]
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
    if tool == "propose":
        return lambda service: service.propose(
            args.in_stable_id, out_deck=args.out_deck, in_deck=args.in_deck, intent=args.intent, top=args.top
        )
    if tool in ("show", "set-show"):
        raise AssertionError(f"{tool} is answered offline in run()")
    if tool in ("validate", "schedule"):
        plan = _read_plan(args.plan)
        return (
            (lambda service: service.validate(plan)) if tool == "validate" else (lambda service: service.schedule(plan))
        )
    return lambda service: service.amend(args.plan_id, args.action, args.bars)


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def _exit_for(code: str) -> int:
    if code in _NO_PAGE_CODES:
        return EXIT_NO_PAGE
    if code in _REFUSED_CODES:
        return EXIT_UNCONFIRMED
    return EXIT_FAILED


def _show(document: dict[str, Any], view: str) -> int:
    """``opendj dj show``: the score and grid print as text, JSON as JSON."""
    try:
        plan = parse_plan(document)
    except PlanError as error:
        _emit({"error": "plan_invalid", "message": str(error)})
        return _exit_for("plan_invalid")
    if view == "json":
        _emit(plan.to_document())
        return EXIT_CONFIRMED
    try:
        text = print_score(plan) if view == "score" else render_grid(plan)
    except ScoreError as error:
        _emit({"error": "plan_invalid", "message": str(error)})
        return _exit_for("plan_invalid")
    sys.stdout.write(text)
    return EXIT_CONFIRMED


def _show_set(planned: SetPlan, view: str) -> int:
    if view == "json":
        _emit(planned.to_json())
        return EXIT_CONFIRMED
    try:
        text = (
            print_set_score(planned)
            if view == "score"
            else "\n".join(render_grid(plan) for plan in planned.transitions)
        )
    except ScoreError as error:
        _emit({"error": "plan_invalid", "message": str(error)})
        return _exit_for("plan_invalid")
    sys.stdout.write(text)
    return EXIT_CONFIRMED


_OWN_PARSERS: dict[str, Callable[[Sequence[str], Path | None], int]] = {
    "try": lambda rest, lock: dj_try_cli.run(rest[1:], lock=lock),
    "pair-transition": lambda rest, lock: dj_pair_cli.run(rest[1:], lock=lock),
    "why": lambda rest, lock: dj_why_cli.run(rest, lock=lock),
    "verdict": lambda rest, lock: dj_why_cli.run(rest, lock=lock),
}


def run(
    rest: Sequence[str],
    *,
    lock: Path | None,
    service_factory: Callable[[Path | None], DjService] | None = None,
) -> int:
    """Run one ``opendj dj`` tool; ``service_factory`` is the tests' seam."""
    # try, pair-transition, why and verdict have their own parsers (try: options plus pick/undo actions).
    if rest and rest[0] in _OWN_PARSERS:
        return _OWN_PARSERS[rest[0]](rest, lock)
    try:
        args = _parser().parse_args(list(rest))
        if args.tool == "show":
            return _show(_read_plan(args.plan), args.view)
        if args.tool == "set-show":
            return _show_set(_read_set(args.set), args.view)
        call = _set_call(args) if args.tool.startswith("set-") else _call(args)
    except _UsageError as error:
        _emit({"error": "usage", "message": str(error)})
        return EXIT_FAILED
    except DjError as error:
        _emit(error.document())
        return _exit_for(error.code)
    if args.tool == "styles":  # the catalog is static: no engine needed
        _emit({"styles": [s.model_dump(mode="json") for s in build_service().list_styles()]})
        return EXIT_CONFIRMED
    if args.tool == "verbs":  # the same document as the MCP list_verbs tool, also static
        _emit({"verbs": describe_verbs()})
        return EXIT_CONFIRMED
    return _run_on_engine(call, lock, service_factory)


def _run_on_engine(
    call: Callable[[DjService], Any],
    lock: Path | None,
    service_factory: Callable[[Path | None], DjService] | None,
) -> int:
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
    # set-next with nothing due answered truthfully but performed nothing.
    return EXIT_UNCONFIRMED if document.get("scheduled") is False else EXIT_CONFIRMED
