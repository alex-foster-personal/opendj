"""``opendj update check|apply|ready|status|install|cancel``: the updater on the
installed CLI (AGENT-13; ``ready``/``status``/``install`` are restart-to-update,
INSTALL-39; ``cancel`` withdraws an install during its 10 s countdown, INSTALL-43).

``ready <artifact.app.tar.gz> [--notes TEXT]`` flags a local build as the
pending update (the tray shows "Restart to update"), ``status`` reads what is
pending, and ``install`` makes the shell install it and waits until the
relaunched app reports the flagged git sha. All three call
:mod:`apps.engine_core.local_update_client`, the same core the MCP tools call.

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

from apps.engine_core.local_update_client import (
    InstallOutcome,
    cancel_via_engine,
    install_via_engine,
    ready_via_engine,
    status_via_engine,
)
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
from apps.opendj_cli.refusal import fail as _fail

CHECK_COMMAND = "check"
APPLY_COMMAND = "apply"
READY_COMMAND = "ready"
STATUS_COMMAND = "status"
INSTALL_COMMAND = "install"
CANCEL_COMMAND = "cancel"
EXIT_USAGE = 1

_USAGE = (
    "usage: opendj update check | opendj update apply [--timeout-s SECONDS] | "
    "opendj update ready <artifact.app.tar.gz> [--notes TEXT] | opendj update status | "
    "opendj update install [--timeout-s SECONDS] | opendj update cancel"
)


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


def _parse_apply(
    tokens: Sequence[str], verb: str = APPLY_COMMAND
) -> tuple[float, str] | tuple[None, str]:
    """``--timeout-s`` in seconds, or the reason the invocation is refused.

    A deadline has to be a real number: ``nan`` parses happily and then
    poisons every comparison it reaches, so a wait bounded by it never ends.
    """
    timeout_s = APPLY_TIMEOUT_S
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token != "--timeout-s":
            return None, f"update {verb}: unexpected argument {token!r}"
        if position + 1 >= len(tokens):
            return None, f"update {verb}: --timeout-s requires a number of seconds"
        raw = tokens[position + 1]
        try:
            timeout_s = float(raw)
        except ValueError:
            return None, f"update {verb}: --timeout-s expects seconds, got {raw!r}"
        if not timeout_s > 0:
            return None, f"update {verb}: --timeout-s must be greater than zero, got {raw!r}"
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


# ----- restart to update (INSTALL-39) --------------------------------------
def _parse_ready(tokens: Sequence[str]) -> tuple[Path, str | None] | str:
    """``<artifact> [--notes TEXT]``, or the reason the invocation is refused."""
    artifact: Path | None = None
    notes: str | None = None
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token == "--notes":
            if position + 1 >= len(tokens):
                return "update ready: --notes requires text"
            notes = tokens[position + 1]
            position += 2
        elif token.startswith("--"):
            return f"update ready: unexpected option {token!r}"
        elif artifact is None:
            artifact = Path(token)
            position += 1
        else:
            return f"update ready: unexpected argument {token!r}"
    if artifact is None:
        return "update ready: name the updater archive, e.g. .../bundle/macos/Open DJ.app.tar.gz"
    return artifact, notes


def _print_reply(document: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return
    pending = document.get("pending") or {}
    lines = [f"state: {document.get('state')}"]
    if pending:
        lines.append(
            f"pending: {pending.get('git_sha')} v{pending.get('app_version')} "
            f"built {pending.get('built_at_utc')} ({pending.get('artifact')})"
        )
    lines.append(f"running: {document.get('running_git_sha_full')}")
    lineage = pending.get("lineage") or {}
    if lineage:
        lines.append(f"lineage: {lineage.get('color_name')} {lineage.get('lines')}")
    warning = (pending.get("descent") or {}).get("warning")
    if warning:
        lines.append(f"[WARN] {warning}")
    if document.get("detail"):
        lines.append(f"detail: {document.get('detail')}")
    print("\n".join(lines))


def _ready(tokens: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    parsed = _parse_ready(tokens)
    if isinstance(parsed, str):
        return _usage(parsed, as_json=as_json)
    origin = _engine(lock, as_json=as_json)
    if isinstance(origin, int):
        return origin
    reply = ready_via_engine(origin.base_url, parsed[0], parsed[1])
    if not reply.ok:
        return _fail(as_json=as_json, code=reply.error or "refused", message=reply.detail or "", exit_code=EXIT_NOT_APPLIED)
    _print_reply(reply.document, as_json=as_json)
    return EXIT_APPLIED


def _status(tokens: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    if tokens:
        return _usage(f"update status: unexpected argument {tokens[0]!r}", as_json=as_json)
    origin = _engine(lock, as_json=as_json)
    if isinstance(origin, int):
        return origin
    reply = status_via_engine(origin.base_url)
    if not reply.ok:
        return _fail(as_json=as_json, code=reply.error or "refused", message=reply.detail or "", exit_code=EXIT_NOT_APPLIED)
    _print_reply(reply.document, as_json=as_json)
    return EXIT_APPLIED


def _install(tokens: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    timeout_s, problem = _parse_apply(tokens, INSTALL_COMMAND)
    if timeout_s is None:
        return _usage(problem, as_json=as_json)
    origin = _engine(lock, as_json=as_json)
    if isinstance(origin, int):
        return origin
    outcome = install_via_engine(origin.base_url, timeout_s, lock_path=lock, progress=_progress)
    print(json.dumps(outcome.document(), indent=2, sort_keys=True) if as_json else _install_text(outcome))
    if not outcome.installed:
        print(f"opendj: {outcome.reason}: {outcome.detail}", file=sys.stderr)
    return outcome.code


def _install_text(outcome: InstallOutcome) -> str:
    return "\n".join(
        [
            f"before: git_sha_full={outcome.before.get('git_sha_full')}",
            f"after: git_sha_full={outcome.after.get('git_sha_full')}",
            f"installed: {'yes' if outcome.installed else f'no ({outcome.reason})'}",
            f"detail: {outcome.detail}",
        ]
    )


def _cancel(tokens: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    """Withdraw the install counting down; the CLI twin of Esc Space x4 (INSTALL-43)."""
    if tokens:
        return _usage(f"update cancel: unexpected argument {tokens[0]!r}", as_json=as_json)
    origin = _engine(lock, as_json=as_json)
    if isinstance(origin, int):
        return origin
    reply = cancel_via_engine(origin.base_url, "opendj update cancel")
    if not reply.ok:
        return _fail(as_json=as_json, code=reply.error or "refused", message=reply.detail or "", exit_code=EXIT_NOT_APPLIED)
    if as_json:
        print(json.dumps(reply.document, indent=2, sort_keys=True))
    else:
        print(f"cancelled: install of {reply.document.get('git_sha_full')} (command_id={reply.document.get('command_id')})")
    return EXIT_APPLIED


_VERBS = {
    CHECK_COMMAND: _check,
    APPLY_COMMAND: _apply,
    READY_COMMAND: _ready,
    STATUS_COMMAND: _status,
    INSTALL_COMMAND: _install,
    CANCEL_COMMAND: _cancel,
}


def run(tokens: Sequence[str], *, as_json: bool, lock: Path | None = None) -> int:
    """Dispatch ``opendj update <verb>``: check, apply, ready, status, install, cancel."""
    if not tokens:
        return _usage(_USAGE, as_json=as_json)
    command, *rest = tokens
    handler = _VERBS.get(command)
    if handler is None:
        return _usage(f"unknown update verb {command!r}; {_USAGE}", as_json=as_json)
    return handler(rest, as_json=as_json, lock=lock)


__all__ = [
    "APPLY_COMMAND",
    "CANCEL_COMMAND",
    "CHECK_COMMAND",
    "EXIT_APPLY_FAILED",
    "EXIT_USAGE",
    "INSTALL_COMMAND",
    "READY_COMMAND",
    "STATUS_COMMAND",
    "run",
]
