"""The ``opendj`` console script: AGENT-05's client of the AGENT-03 bus.

    opendj state [--json]
    opendj deck 1 play | pause | cue | seek 42000
    opendj eq 2 low 0.2 --over 4beats
    opendj do "load 1 <stable-id>" then "play 1" and "play 2"
    opendj --list-verbs [--json]

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
from apps.opendj_cli.orders import (
    SINGLE,
    Group,
    parse_script,
    ramp,
    ramp_deadline_s,
    single,
)
from apps.opendj_cli.origin import EngineNotRunning, EngineOrigin, resolve_origin
from apps.opendj_cli.verbs import (
    DURATION_ANCHORS,
    InvocationError,
    describe_verbs,
    parse_duration,
    parse_invocation,
)

# Every way the engine can refuse an order, so one handler maps them all.
_REFUSALS = (NoPerformancePage, OrderTimedOut, OrderRejected, MalformedResult)

_STATE_COMMAND = "state"
_SCRIPT_COMMAND = "do"


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
    except _REFUSALS as error:
        return _refusal(args, error)
    if args.json:
        print(json.dumps(mirror, indent=2, sort_keys=True))
    else:
        print(_state_text(mirror))
    return EXIT_CONFIRMED


def _run_script(args: argparse.Namespace, origin: EngineOrigin, items: Sequence[str]) -> int:
    groups = parse_script(items)
    orders = [(group, group.order()) for group in groups]
    return _dispatch(
        args,
        origin,
        orders,
    )


def _run_invocation(args: argparse.Namespace, origin: EngineOrigin, tokens: Sequence[str]) -> int:
    invocation = parse_invocation(tokens)
    if args.over is None and (args.anchor is not None or args.clock is not None):
        raise InvocationError("--anchor and --clock only mean something with --over")
    over = None
    if args.over is None:
        order = single(invocation)
    else:
        over = parse_duration(args.over, anchor=args.anchor, clock=_clock(args.clock))
        order = ramp(invocation, over)
    group = Group(kind=SINGLE, invocations=(invocation,))
    return _dispatch(args, origin, [(group, order)], over=over)


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
            run.results.append({"kind": group.kind, "skipped": group.label()})
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
            client.timeout_s = ramp_deadline_s(over, args.timeout, mirror)
        run = _post_all(client, orders, args.settle)
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
        return [f"skipped: {entry['skipped']} (an earlier step failed)"]
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


def _state_text(mirror: dict[str, Any]) -> str:
    lines = [
        f"context: {mirror.get('context_state', 'absent')}",
        f"open: {mirror.get('client_open', 'absent')}",
    ]
    master = mirror.get("master")
    if isinstance(master, dict):
        lines.append(
            f"master: muted={master.get('muted', 'absent')} "
            f"level={master.get('level', 'absent')} rms={master.get('rms', 'absent')}"
        )
    mixer = mirror.get("mixer")
    if isinstance(mixer, dict):
        lines.append(f"crossfader: {mixer.get('crossfader', 'absent')}")
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


def _print_verbs(as_json: bool) -> None:
    rows = describe_verbs()
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    name_width = max(len(row["verb"]) for row in rows)
    command_width = max(len(row["command"]) for row in rows)
    usage_width = max(len(row["usage"]) for row in rows)
    print(f"{'verb':<{name_width}}  {'bus command':<{command_width}}  usage")
    for row in rows:
        print(
            f"{row['verb']:<{name_width}}  {row['command']:<{command_width}}  "
            f"{row['usage']:<{usage_width}}  {_verb_flags(row)}".rstrip()
        )


def _verb_flags(row: dict[str, Any]) -> str:
    flags = []
    if row["quick_draw"]:
        flags.append("quick-draw " + ",".join(row["quick_draw"]))
    if row["rampable"]:
        flags.append("--over")
    if row["confirmed_against_mirror"]:
        flags.append("mirror-confirmed")
    return "  ".join(flags)


# ----- entry point ---------------------------------------------------------

def _head(tokens: Sequence[str]) -> tuple[str, list[str]]:
    """Split ``state``/``do``/verb off the invocation, refusing an empty one."""
    if not tokens:
        raise InvocationError("no command given; run opendj --help")
    head, *rest = tokens
    if head == _STATE_COMMAND and rest:
        raise InvocationError(f"state takes no arguments, got {rest[0]!r}")
    return head, rest


def main(argv: Sequence[str] | None = None) -> int:
    tokens = sys.argv[1:] if argv is None else list(argv)
    args = _parser(as_json="--json" in tokens).parse_args(tokens)
    if args.list_verbs:
        _print_verbs(args.json)
        return EXIT_CONFIRMED
    try:
        head, rest = _head(args.invocation)
        origin = resolve_origin(args.lock)
        if head == _STATE_COMMAND:
            return _run_state(args, origin)
        if head == _SCRIPT_COMMAND:
            return _run_script(args, origin, rest)
        return _run_invocation(args, origin, args.invocation)
    except InvocationError as error:
        return _fail(args, "usage", str(error), EXIT_FAILED)
    except EngineNotRunning as error:
        return _fail(args, "engine_not_running", str(error), EXIT_NO_ENGINE)


if __name__ == "__main__":
    sys.exit(main())
