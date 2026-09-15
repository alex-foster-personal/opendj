"""``opendj update check`` and ``opendj update apply``: the updater on the
installed CLI (AGENT-13).

The packaged app ships ``~/.local/bin/opendj`` and ``opendj mcp``. Issue
#2924 delivered the updater as ``POST /api/v1/update/apply`` plus
``python -m apps.engine_core.update_channel``, and on a machine that holds
only the installed app the second half is unreachable: there is no checkout
to run a module out of. That left an agent able to send a raw
``opendj api POST /api/v1/update/apply`` and then poll ``/api/v1/build-info``
by hand, which is the agent-native parity gap this module closes.

REUSE, NOT A SECOND COPY. Both verbs call :mod:`apps.engine_core.
update_channel`: ``check_via_engine`` for the question, and
``apply_via_engine`` for the whole enqueue-install-relaunch-compare path.
``apply`` is the call whose correctness matters most, and two implementations
of it would drift the first time one of them was fixed.

Exit codes:

===  ==========================================================================
0    check: the channel answered ``up-to-date`` or ``update-available``, the
     only two statuses a caller may act on. apply: the relaunched app reports
     the announced ``app_version`` AND a different ``git_sha_full``.
1    the invocation was refused before anything was sent.
2    check: any other status, named with its detail. apply: the install did
     not happen, with the reason and the channel's own status and detail.
3    apply: the shell reported the install itself failed (an unverified
     signature, say) and printed its own error.
===  ==========================================================================
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.engine_core.update_channel import (
    APPLY_TIMEOUT_S,
    EXIT_APPLIED,
    EXIT_APPLY_FAILED,
    EXIT_NOT_APPLIED,
    ApplyOutcome,
    apply_via_engine,
    check_via_engine,
)
from apps.opendj_cli.origin import EngineNotRunning, EngineOrigin, resolve_origin

CHECK_COMMAND = "check"
APPLY_COMMAND = "apply"
EXIT_USAGE = 1

_USAGE = "usage: opendj update check | opendj update apply [--timeout-s SECONDS]"


def _fail(*, as_json: bool, code: str, message: str, exit_code: int) -> int:
    """Report a refusal on stderr, or as JSON on stdout, and return its code.

    The same envelope ``opendj install-cli`` uses, so an agent parsing one
    installed-app verb can parse them all.
    """
    if as_json:
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


def _usage(problem: str, *, as_json: bool) -> int:
    return _fail(as_json=as_json, code="usage", message=problem, exit_code=EXIT_USAGE)


def _engine(lock: Path | None, *, as_json: bool) -> EngineOrigin | int:
    """The running app's origin, or the exit-2 refusal naming the lock file."""
    try:
        return resolve_origin(lock)
    except EngineNotRunning as error:
        return _fail(
            as_json=as_json,
            code="engine_not_running",
            message=str(error),
            exit_code=EXIT_NOT_APPLIED,
        )


def _parse_apply(tokens: Sequence[str]) -> tuple[float, str] | tuple[None, str]:
    """``--timeout-s`` in seconds, or the reason the invocation is refused.

    A deadline has to be a real number: ``nan`` parses happily and then
    poisons every comparison it reaches, so a wait bounded by it never ends.
    """
    timeout_s = APPLY_TIMEOUT_S
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token != "--timeout-s":
            return None, f"update apply: unexpected argument {token!r}"
        if position + 1 >= len(tokens):
            return None, "update apply: --timeout-s requires a number of seconds"
        raw = tokens[position + 1]
        try:
            timeout_s = float(raw)
        except ValueError:
            return None, f"update apply: --timeout-s expects seconds, got {raw!r}"
        if not timeout_s > 0:
            return None, f"update apply: --timeout-s must be greater than zero, got {raw!r}"
        position += 2
    return timeout_s, ""


def _check(tokens: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    if tokens:
        return _usage(f"update check: unexpected argument {tokens[0]!r}", as_json=as_json)
    origin = _engine(lock, as_json=as_json)
    if isinstance(origin, int):
        return origin
    outcome = check_via_engine(origin.base_url)
    if as_json:
        print(json.dumps(outcome.document, indent=2, sort_keys=True))
    else:
        print(_check_text(outcome.status, outcome.detail, outcome.document))
    if not outcome.actionable:
        print(f"opendj: {outcome.status}: {outcome.detail}", file=sys.stderr)
        return EXIT_NOT_APPLIED
    return EXIT_APPLIED


def _apply(tokens: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    timeout_s, problem = _parse_apply(tokens)
    if timeout_s is None:
        return _usage(problem, as_json=as_json)
    origin = _engine(lock, as_json=as_json)
    if isinstance(origin, int):
        return origin
    outcome = apply_via_engine(
        origin.base_url,
        timeout_s,
        lock_path=lock,
        progress=_progress,
    )
    if as_json:
        print(json.dumps(_apply_document(outcome), indent=2, sort_keys=True))
    else:
        print(_apply_text(outcome))
    if outcome.code != EXIT_APPLIED:
        print(f"opendj: {outcome.reason}: {outcome.detail}", file=sys.stderr)
    return outcome.code


def _progress(message: str) -> None:
    """Progress on stderr, so stdout stays one parseable document."""
    print(f"[APPLY] {message}", file=sys.stderr)


def _check_text(status: str, detail: str | None, document: dict[str, Any]) -> str:
    lines = [f"status: {status}"]
    lines.extend(
        f"{key}: {document.get(key)}"
        for key in ("current_version", "available_version")
    )
    if detail is not None:
        lines.append(f"detail: {detail}")
    return "\n".join(lines)


def _apply_document(outcome: ApplyOutcome) -> dict[str, Any]:
    return {
        "applied": outcome.applied,
        "reason": outcome.reason,
        "status": outcome.status,
        "detail": outcome.detail,
        "available_version": outcome.advertised_version,
        "command_id": outcome.command_id,
        "before": outcome.before,
        "after": outcome.after,
        "exit_code": outcome.code,
    }


def _apply_text(outcome: ApplyOutcome) -> str:
    lines = [
        f"before: app_version={outcome.before.get('app_version')} "
        f"git_sha_full={outcome.before.get('git_sha_full')}",
        f"after: app_version={outcome.after.get('app_version')} "
        f"git_sha_full={outcome.after.get('git_sha_full')}",
    ]
    if outcome.applied:
        lines.append(f"applied: {outcome.advertised_version}")
    else:
        lines.append(f"applied: no ({outcome.reason})")
        lines.append(f"detail: {outcome.detail}")
    return "\n".join(lines)


def run(tokens: Sequence[str], *, as_json: bool, lock: Path | None = None) -> int:
    """Dispatch ``opendj update <verb>``; the verbs are ``check`` and ``apply``."""
    if not tokens:
        return _usage(_USAGE, as_json=as_json)
    command, *rest = tokens
    if command == CHECK_COMMAND:
        return _check(rest, as_json=as_json, lock=lock)
    if command == APPLY_COMMAND:
        return _apply(rest, as_json=as_json, lock=lock)
    return _usage(f"unknown update verb {command!r}; {_USAGE}", as_json=as_json)


__all__ = [
    "APPLY_COMMAND",
    "CHECK_COMMAND",
    "EXIT_APPLY_FAILED",
    "EXIT_USAGE",
    "run",
]
