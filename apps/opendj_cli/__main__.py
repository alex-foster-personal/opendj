"""The ``opendj`` console script: AGENT-05's client of the AGENT-03 bus.

    opendj state [--json]
    opendj status [--json]
    opendj deck 1 play | pause | cue | seek 42000
    opendj eq 2 low 0.2 --over 4beats
    opendj do "load 1 <stable-id>" then "play 1" and "play 2"
    opendj api GET /api/v1/smartlists
    opendj api POST /api/v1/smartlists --json '{"name":"...","rule":{...}}'
    opendj --list-verbs [--json]
    opendj install-cli [--target ~/.local/bin/opendj]
    opendj update check | opendj update apply [--timeout-s SECONDS]

Exit codes are documented in :mod:`apps.opendj_cli`; the load-bearing ones are
2 (no engine, and the message names the lock file that was checked) and 4 (the
page accepted the order but the mirror never confirmed it).

Every command that touches the interface goes through
``POST /api/v1/commands``, so the CLI is a client of the same bus a button
press uses and not a second path into the engine.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from apps.opendj_cli import (
    EXIT_CONFIRMED,
    EXIT_FAILED,
    EXIT_NO_ENGINE,
    EXIT_NO_PAGE,
    EXIT_TIMEOUT,
    EXIT_UNCONFIRMED,
)
from apps.opendj_cli.catalog import DECK_VALUES
from apps.opendj_cli.client import (
    ORDER_TIMEOUT_S,
    EngineClient,
    MalformedResult,
    NoPerformancePage,
    OrderRejected,
    OrderTimedOut,
)
from apps.opendj_cli.confirm import (
    SETTLE_POLL_S,
    SETTLE_TIMEOUT_S,
    Check,
    checks_for,
    final_checks,
    verdict,
)
from apps.opendj_cli.helptext import LIBRARY_EPILOG, print_verbs
from apps.opendj_cli.orders import (
    SINGLE,
    Group,
    clock_beat_s,
    parse_script,
    ramp,
    ramp_deadline_s,
    single,
    slowed_since,
)
from apps.opendj_cli.origin import (
    EngineIdentityMismatch,
    EngineNotRunning,
    EngineOrigin,
    resolve_verified_origin,
)
from apps.opendj_cli.verbs import (
    DURATION_ANCHORS,
    InvocationError,
    parse_duration,
    parse_invocation,
)

# Every way the engine can refuse an order, so one handler maps them all.
_REFUSALS = (NoPerformancePage, OrderTimedOut, OrderRejected, MalformedResult)

_STATE_COMMAND = "state"
_STATUS_COMMAND = "status"
_OPEN_COMMAND = "open"
_SCRIPT_COMMAND = "do"
_TRACK_COMMAND = "track"
_AUDIO_OUTPUT_HEALTH_COMMAND = "audio_output_health"
_AUDIO_SWITCH_OUTPUT_COMMAND = "audio_switch_output"
_FEEDBACK_COMMAND = "feedback"
_API_COMMAND = "api"
_INSTALL_COMMAND = "install-cli"
_MCP_COMMAND = "mcp"
_UPDATE_COMMAND = "update"


class _Parser(argparse.ArgumentParser):
    """An argparse that exits 1, because 2 is the engine-not-running code.

    ``--json`` has to be read off the raw argv rather than the namespace:
    argparse calls ``error`` while PARSING, so on ``--json --anchor bogus``
    there is no namespace yet and the flag the caller passed would otherwise be
    ignored. A machine caller that asked for JSON gets JSON for every refusal,
    including the ones raised before the namespace exists.
    """

    def __init__(self, *args: Any, as_json: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.as_json = as_json

    def error(self, message: str) -> NoReturn:
        if self.as_json:
            print(
                json.dumps(
                    {
                        "error": {"code": "usage", "message": message},
                        "exit_code": EXIT_FAILED,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            self.print_usage(sys.stderr)
            print(f"opendj: {message}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED)


def _seconds(raw: str) -> float:
    """A deadline in seconds: finite and not negative.

    ``float("nan")`` parses happily and then poisons every comparison it
    reaches: `time.monotonic() >= deadline` is False for a NaN deadline no
    matter how long the CLI has waited, so `--settle nan` polls the mirror
    forever rather than exiting 4. `inf` wedges the same loop by being honest
    about it, and a negative deadline is a typo, not an instruction.
    """
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number of seconds") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{raw!r} is not a finite number of seconds")
    if value < 0:
        raise argparse.ArgumentTypeError(f"a deadline cannot be negative, got {raw!r}")
    return value


def _parser(as_json: bool = False) -> _Parser:
    parser = _Parser(
        as_json=as_json,
        prog="opendj",
        description="Drive the running Open DJ performance surface over the AGENT-03 bus.",
        epilog=LIBRARY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("invocation", nargs="*", help="a verb and its arguments")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--list-verbs",
        action="store_true",
        help="name every command the bus accepts, then exit",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="engine lock file to read (default: $OPENDJ_LIVE_LOCK_PATH, else the app's)",
    )
    parser.add_argument(
        "--timeout",
        type=_seconds,
        default=ORDER_TIMEOUT_S,
        help=f"seconds to wait for a page to complete an order (default {ORDER_TIMEOUT_S})",
    )
    parser.add_argument(
        "--settle",
        type=_seconds,
        default=SETTLE_TIMEOUT_S,
        help=f"seconds to wait for the mirror to confirm an order (default {SETTLE_TIMEOUT_S})",
    )
    parser.add_argument("--over", default=None, help="ramp over a Duration, e.g. 4beats")
    parser.add_argument("--anchor", choices=DURATION_ANCHORS, default=None)
    parser.add_argument(
        "--clock", default=None, help="ramp clock deck: master or 1..4 (default master)"
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="state.db override for opendj track (read-only analysis surface)",
    )
    return parser


# ----- output --------------------------------------------------------------

def _fail(args: argparse.Namespace, code: str, message: str, exit_code: int) -> int:
    """Report a refusal on stderr (or as JSON on stdout) and return its code."""
    if args.json:
        print(
            json.dumps(
                {"error": {"code": code, "message": message}, "exit_code": exit_code},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj: {message}", file=sys.stderr)
    return exit_code


def _step_counts(steps: Sequence[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for step in steps:
        status = str(step.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    rendered = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    return rendered if rendered else "no steps"


def _step_errors(steps: Sequence[dict[str, Any]]) -> list[str]:
    return [
        str(step["error"])
        for step in steps
        if step.get("status") == "failed" and step.get("error") is not None
    ]


# ----- commands ------------------------------------------------------------

def _run_state(args: argparse.Namespace, origin: EngineOrigin) -> int:
    client = EngineClient(origin=origin, timeout_s=args.timeout)
    try:
        mirror = client.mirror()
        prefs = client.ui_prefs()
    except _REFUSALS as error:
        return _refusal(args, error)
    payload = {
        **mirror,
        "persisted": {"master_muted": prefs["master_muted"]},
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_state_text(mirror, prefs))
    return EXIT_CONFIRMED


def _run_open(args: argparse.Namespace, origin: EngineOrigin, rest: Sequence[str]) -> int:
    from apps.opendj_cli.shell_navigate import open_performance

    if len(rest) != 1:
        raise InvocationError("usage: opendj open performance")
    target = rest[0]
    if target not in ("performance", "/performance"):
        raise InvocationError(
            f"unknown route {target!r}; only performance (/performance) is supported"
        )
    try:
        result = open_performance(origin)
    except _REFUSALS as error:
        return _refusal(args, error)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"opened {result['route']} (client_open={result['client_open']})")
    return EXIT_CONFIRMED


def _plan(
    args: argparse.Namespace, head: str, rest: Sequence[str]
) -> tuple[list[tuple[Group, dict[str, Any]]], dict[str, Any] | None]:
    """Read the invocation into orders, touching nothing outside this process.

    Deliberately separate from dispatch, and run BEFORE the engine is resolved.
    `opendj frobnicate` with the app closed used to exit 2 (engine not running),
    so a typo was indistinguishable from a stopped app and no caller could
    check a command's syntax offline. What the CLI can answer on its own it
    answers on its own.
    """
    if head == _SCRIPT_COMMAND:
        groups = parse_script(rest)
        return [(group, group.order()) for group in groups], None
    invocation = parse_invocation(args.invocation)
    if args.over is None and (args.anchor is not None or args.clock is not None):
        raise InvocationError("--anchor and --clock only mean something with --over")
    group = Group(kind=SINGLE, invocations=(invocation,))
    if args.over is None:
        return [(group, single(invocation))], None
    over = parse_duration(args.over, anchor=args.anchor, clock=_clock(args.clock))
    return [(group, ramp(invocation, over))], over


def _clock(raw: str | None) -> int | str | None:
    if raw is None:
        return None
    if raw == "master":
        return "master"
    if raw.lstrip("-").isdigit() and int(raw) in DECK_VALUES:
        return int(raw)
    raise InvocationError(f"--clock must be master or a deck 1..4, got {raw!r}")


def _refusal(args: argparse.Namespace, error: Exception) -> int:
    """Map one client refusal to its documented exit code."""
    message = str(error)
    if isinstance(error, NoPerformancePage):
        return _fail(args, "no_performance_page", message, EXIT_NO_PAGE)
    if isinstance(error, OrderTimedOut):
        return _fail(args, "order_timeout", message, EXIT_TIMEOUT)
    if isinstance(error, MalformedResult):
        return _fail(args, "malformed_result", message, EXIT_FAILED)
    return _fail(args, "order_rejected", message, EXIT_FAILED)


#: Weakest wins when a script's groups disagree. A run is only as strong as
#: its weakest group: one unconfirmed group makes the whole run unconfirmed,
#: and one merely accepted group means the run cannot claim confirmed.
_VERDICTS = ("confirmed", "accepted", "unconfirmed")


@dataclass
class _Run:
    """What one script run did, and whether the mirror agreed as it went."""

    results: list[dict[str, Any]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(step.get("status") == "failed" for step in _steps(self.results))

    @property
    def halted(self) -> bool:
        """Has the run hit something that must stop the REST of the script?

        Both halves matter. A failed step is a page-reported error; an
        unconfirmed group is a page that claimed success the mirror will not
        corroborate. Carrying on past the second is the more dangerous of the
        two, because the later commands then run against a deck whose state is
        NOT the one the script asked for.
        """
        return self.failed or bool(self.failures)

    @property
    def halt_reason(self) -> str:
        """Why the rest of the script was skipped, in the caller's terms.

        The two halts read identically from the outside and do not mean the
        same thing: one is the page reporting an error, the other is a page
        reporting success the mirror will not corroborate. An agent deciding
        what to do next needs to know which it got.
        """
        return (
            "an earlier step failed" if self.failed
            else "an earlier group was not confirmed"
        )

    def verdict(self) -> str:
        if not self.verdicts:
            return "accepted"
        return max(self.verdicts, key=_VERDICTS.index)


def _post_all(
    client: EngineClient,
    orders: Sequence[tuple[Group, dict[str, Any]]],
    settle_s: float,
) -> _Run:
    """Run the groups in order, confirming EACH against the mirror it left.

    Confirming once at the end cannot work, because a later command's side
    effects are not confined to the paths an earlier command named. Dropping
    checks that share a path fixed `do "play 1" then "pause 1"`, but production
    `unload` replaces the whole slot with `_emptyDeckState`, so
    `do "play 1" then "unload 1"` still carried `playing == true` into a final
    mirror that correctly reads false, and a perfect run exited 4.

    Checking each group against the mirror it produced needs no model of what a
    command touches, which is the point: the alternative is a second copy of
    production's side effects, and a second copy is exactly what drifts.

    A group that does not confirm stops the script exactly as a failed step
    does. `do "load 1 <id>" then "play 1"` is the case that matters: if the
    load never lands, the deck still holds the PREVIOUS track, and dispatching
    `play` there puts the wrong music into the room before the CLI exits 4.
    """
    run = _Run()
    for group, order in orders:
        if run.halted:
            run.results.append(
                {
                    "kind": group.kind,
                    "skipped": group.label(),
                    "because": run.halt_reason,
                }
            )
            continue
        result = client.post_order(order)
        run.results.append(
            {
                "kind": group.kind,
                "commands": group.label(),
                "steps": _steps_of(result),
                "mirror_delta": result.get("mirror_delta", {}),
            }
        )
        checks = final_checks(
            [
                check
                for invocation in group.invocations
                for check in checks_for(invocation.verb, invocation.command)
            ]
        )
        run.checks.extend(checks)
        state, failures = _settle(client, checks, settle_s)
        run.verdicts.append(state)
        run.failures.extend(failures)
    return run


def _steps(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [step for entry in entries for step in entry.get("steps", [])]


def _steps_of(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = result.get("steps")
    if not isinstance(steps, list):
        raise MalformedResult(
            f"the page returned no per-step result document: {sorted(result)}"
        )
    return steps


def _dispatch(
    args: argparse.Namespace,
    origin: EngineOrigin,
    orders: Sequence[tuple[Group, dict[str, Any]]],
    over: dict[str, Any] | None = None,
) -> int:
    client = EngineClient(origin=origin, timeout_s=args.timeout)
    beat_s: float | None = None
    try:
        # The opening mirror read proves a page is there AND supplies the grid.
        # It runs on its own short timeout, so reading it before the order
        # deadline is sized costs nothing and is the only way to size that
        # deadline from the tempo the page will really ramp at.
        mirror = client.mirror()
        if over is not None:
            # The page holds a ramp order open for the ramp's whole duration,
            # so the deadline that guards against a WEDGED page must not fire
            # during a working one.
            beat_s = clock_beat_s(over, mirror)
            client.timeout_s = ramp_deadline_s(over, args.timeout, mirror)
        run = _post_all(client, orders, args.settle)
    except OrderTimedOut as error:
        return _fail(
            args, "order_timeout", _timeout_message(client, error, over, beat_s), EXIT_TIMEOUT
        )
    except _REFUSALS as error:
        return _refusal(args, error)

    state = run.verdict()
    results, failed, failures = run.results, run.failed, run.failures
    document = {
        "groups": results,
        "checks": [check.description for check in run.checks],
        "verdict": state,
        "unconfirmed": list(failures),
        # How long the CLI was willing to wait for the page to answer. A ramp
        # raises it above --timeout, so an agent can see the deadline it
        # actually got rather than infer it.
        "request_timeout_s": client.timeout_s,
        # The tempo that deadline was sized against, so an agent can see WHY it
        # is the length it is, and notice when the clock has moved since.
        "ramp_clock_bpm": None if beat_s is None else 60.0 / beat_s,
    }
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(_dispatch_text(document))
    if failed:
        return EXIT_FAILED
    if state == "unconfirmed":
        return EXIT_UNCONFIRMED
    return EXIT_CONFIRMED


def _timeout_message(
    client: EngineClient,
    error: OrderTimedOut,
    over: dict[str, Any] | None,
    beat_s: float | None,
) -> str:
    """The timeout, plus the reason for it when the CLI can name one.

    A deadline sized off the grid is sized off the grid AT DISPATCH, and a deck
    slowed after that travels the same musical distance in more wall time. The
    CLI cannot extend a request already in flight (`POST /api/v1/commands` is
    held open by the engine until the page answers, and there is no
    order-status route to poll), so the deadline stays a snapshot. What it can
    do is stop reporting a slowed clock as though the page were wedged: those
    demand opposite responses, and only one of them is fixed by a longer
    --timeout.
    """
    message = str(error)
    if over is None or beat_s is None:
        return message
    try:
        mirror = client.mirror()
    except _REFUSALS:
        # The timeout is the finding; a second failure reading the mirror must
        # not replace it with a different error.
        return message
    slowed = slowed_since(over, beat_s, mirror)
    return message if slowed is None else f"{message}. {slowed}"


def _settle(
    client: EngineClient, checks: Sequence[Check], timeout_s: float
) -> tuple[str, tuple[str, ...]]:
    """Poll the mirror until every check passes or the settle deadline passes."""
    deadline = time.monotonic() + timeout_s
    mirror = client.mirror()
    while True:
        state, failures = verdict(checks, mirror)
        if state != "unconfirmed" or time.monotonic() >= deadline:
            return state, failures
        time.sleep(SETTLE_POLL_S)
        mirror = client.mirror()


def _group_text(entry: Mapping[str, Any]) -> list[str]:
    if "skipped" in entry:
        return [f"skipped: {entry['skipped']} ({entry['because']})"]
    changed = entry.get("mirror_delta", {}).get("changed")
    lines = [
        f"{entry['kind']}: {entry['commands']}",
        f"  steps: {_step_counts(entry['steps'])}",
        f"  mirror delta: {json.dumps(changed, sort_keys=True) if changed else 'none'}",
    ]
    lines.extend(f"  error: {error}" for error in _step_errors(entry["steps"]))
    return lines


def _dispatch_text(document: dict[str, Any]) -> str:
    lines: list[str] = []
    for entry in document["groups"]:
        lines.extend(_group_text(entry))
    state = document["verdict"]
    if state == "confirmed":
        lines.append(f"verdict: confirmed ({'; '.join(document['checks'])})")
    elif state == "unconfirmed":
        lines.append(
            "verdict: unconfirmed - the page accepted the order but the mirror says "
            f"otherwise ({'; '.join(document['unconfirmed'])})"
        )
    else:
        lines.append(
            "verdict: accepted - the page claimed the order and reported no error "
            "and no deck's presentation clock is lagging; nothing in the mirror "
            "names the control this command moves, so acceptance is all that is "
            "claimed"
        )
    return "\n".join(lines)


def _state_text(mirror: dict[str, Any], prefs: dict[str, Any] | None = None) -> str:
    lines = [
        f"context: {mirror.get('context_state', 'absent')}",
        f"open: {mirror.get('client_open', 'absent')}",
    ]
    # AGENT-22: engine-observed; "stale" means agent orders will not run here.
    loop_state = mirror.get("order_loop_state")
    if loop_state is None:
        lines.append("order loop: absent")
    else:
        lines.append(f"order loop: {loop_state} (last poll {mirror.get('last_order_poll_at')})")
    master = mirror.get("master")
    if isinstance(master, dict):
        lines.append(
            f"master: muted={master.get('muted', 'absent')} "
            f"level={master.get('level', 'absent')} rms={master.get('rms', 'absent')}"
        )
    if prefs is not None:
        lines.append(f"persisted: master_muted={prefs.get('master_muted', 'absent')}")
    mixer = mirror.get("mixer")
    if isinstance(mixer, dict):
        lines.append(f"crossfader: {mixer.get('crossfader', 'absent')}")
    transition = mirror.get("transition")
    if isinstance(transition, dict) and "state" in transition:
        lines.append(f"transition: {transition['state']}")
    else:
        lines.append("transition: absent")
    master_deck = mirror.get("master_deck")
    master_mode = mirror.get("master_mode", "absent")
    deck_label = master_deck if master_deck is not None else "none"
    lines.append(f"master_deck: {deck_label} mode={master_mode}")
    decks = mirror.get("decks")
    if isinstance(decks, dict):
        for deck_id in sorted(decks, key=str):
            deck = decks[deck_id]
            if not isinstance(deck, dict):
                lines.append(f"deck {deck_id}: {deck!r}")
                continue
            position = deck.get("position")
            ms = position.get("ms") if isinstance(position, dict) else None
            bars = position.get("bars_beats") if isinstance(position, dict) else None
            clock = deck.get("presentation_clock")
            settled = (
                clock.get("desired_revision") == clock.get("presented_revision")
                if isinstance(clock, dict)
                else "absent"
            )
            lines.append(
                f"deck {deck_id}: {deck.get('title') or 'empty'} | playing="
                f"{deck.get('playing', 'absent')} audible={deck.get('audible', 'absent')} "
                f"bpm={deck.get('bpm', 'absent')} position={ms}ms ({bars}) "
                f"presented={settled}"
            )
    toasts = mirror.get("toasts")
    if isinstance(toasts, list):
        lines.append(f"toasts: {len(toasts)}")
        lines.extend(f"  {toast}" for toast in toasts)
    return "\n".join(lines)


# ----- entry point ---------------------------------------------------------

def _head(tokens: Sequence[str]) -> tuple[str, list[str]]:
    """Split ``state``/``do``/verb off the invocation, refusing an empty one."""
    if not tokens:
        raise InvocationError("no command given; run opendj --help")
    head, *rest = tokens
    if head == _STATE_COMMAND and rest:
        raise InvocationError(f"state takes no arguments, got {rest[0]!r}")
    if head == _STATUS_COMMAND and rest:
        raise InvocationError(f"status takes no arguments, got {rest[0]!r}")
    if head == _OPEN_COMMAND and not rest:
        raise InvocationError("usage: opendj open performance")
    return head, rest


def _split_subcommand(tokens: Sequence[str], name: str) -> tuple[list[str], list[str]] | None:
    """Return global argv and subcommand argv when ``name`` is the subcommand.

    Everything after the subcommand is handed to its own module, including
    flags this parser does not know: ``api`` carries a request-body ``--json``
    that would collide with this module's output flag, and ``update`` carries
    ``--timeout-s``. Both would be refused as "unrecognized arguments" before
    their module ever saw them.
    """
    if name not in tokens:
        return None
    index = tokens.index(name)
    return list(tokens[:index]), list(tokens[index + 1 :])


_STANDALONE_COMMANDS = frozenset(
    {
        _TRACK_COMMAND,
        _FEEDBACK_COMMAND,
        _AUDIO_OUTPUT_HEALTH_COMMAND,
        _AUDIO_SWITCH_OUTPUT_COMMAND,
    }
)


def _run_standalone(head: str, rest: list[str], args: argparse.Namespace) -> int:
    """Run a subcommand whose own module parses ``rest``; keeps ``main`` under its branch cap."""
    if head == _TRACK_COMMAND:
        from apps.opendj_cli import track_cli

        return track_cli.run(rest, as_json=args.json, state_db=args.state_db)
    if head == _FEEDBACK_COMMAND:
        from apps.opendj_cli import feedback_cli

        return feedback_cli.run(rest, as_json=args.json, lock=args.lock)
    from apps.opendj_cli import audio_output_health_cli

    if head == _AUDIO_OUTPUT_HEALTH_COMMAND:
        return audio_output_health_cli.run_health(rest, as_json=args.json, lock=args.lock)
    return audio_output_health_cli.run_switch_output(rest, as_json=args.json, lock=args.lock)


def main(argv: Sequence[str] | None = None) -> int:
    tokens = sys.argv[1:] if argv is None else list(argv)
    mcp_split = _split_subcommand(tokens, _MCP_COMMAND)
    if mcp_split is not None:
        global_tokens, mcp_tokens = mcp_split
        args = _parser(as_json="--json" in global_tokens).parse_args(
            [*global_tokens, _MCP_COMMAND]
        )
        if args.list_verbs:
            print_verbs(args.json)
            return EXIT_CONFIRMED
        from apps.opendj_cli import mcp_cli

        return mcp_cli.run(mcp_tokens, lock=args.lock)
    update_split = _split_subcommand(tokens, _UPDATE_COMMAND)
    if update_split is not None:
        global_tokens, update_tokens = update_split
        args = _parser(as_json="--json" in global_tokens).parse_args(
            [*global_tokens, _UPDATE_COMMAND]
        )
        if args.list_verbs:
            print_verbs(args.json)
            return EXIT_CONFIRMED
        from apps.opendj_cli import update_cli

        return update_cli.run(update_tokens, as_json=args.json, lock=args.lock)
    api_split = _split_subcommand(tokens, _API_COMMAND)
    if api_split is not None:
        global_tokens, api_tokens = api_split
        args = _parser(as_json="--json" in global_tokens).parse_args(
            [*global_tokens, _API_COMMAND]
        )
        if args.list_verbs:
            print_verbs(args.json)
            return EXIT_CONFIRMED
        from apps.opendj_cli import api_cli

        return api_cli.run(api_tokens, as_json=args.json, lock=args.lock)
    args = _parser(as_json="--json" in tokens).parse_args(tokens)
    if args.list_verbs:
        print_verbs(args.json)
        return EXIT_CONFIRMED
    try:
        head, rest = _head(args.invocation)
        if head == _INSTALL_COMMAND:
            from apps.opendj_cli import install_cli

            return install_cli.run(rest, as_json=args.json)
        if head == _STATE_COMMAND:
            return _run_state(args, resolve_verified_origin(args.lock))
        if head == _STATUS_COMMAND:
            from apps.opendj_cli import status_cli

            return status_cli.run(rest, as_json=args.json, lock=args.lock)
        if head == _OPEN_COMMAND:
            return _run_open(args, resolve_verified_origin(args.lock), rest)
        if head in _STANDALONE_COMMANDS:
            return _run_standalone(head, rest, args)
        orders, over = _plan(args, head, rest)
        return _dispatch(args, resolve_verified_origin(args.lock), orders, over)
    except InvocationError as error:
        return _fail(args, "usage", str(error), EXIT_FAILED)
    except EngineIdentityMismatch as error:
        return _fail(args, "engine_identity_mismatch", str(error), EXIT_NO_ENGINE)
    except EngineNotRunning as error:
        return _fail(args, "engine_not_running", str(error), EXIT_NO_ENGINE)


if __name__ == "__main__":
    sys.exit(main())
