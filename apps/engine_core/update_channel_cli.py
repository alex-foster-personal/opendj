"""The ``python -m apps.engine_core.update_channel`` command line.

Part of the update channel split out of one oversized module (#9688). Import
from ``apps.engine_core.update_channel``, the public facade, not from here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from apps.engine_core.build_info import (
    BuildIdentity,
    BuildInfoUnavailable,
    resolve_build_info,
)
from apps.engine_core.update_channel_contract import (
    ANSWERED,
    APPLY_TIMEOUT_S,
    CHECKED_FEED_FILE,
    EXIT_APPLIED,
    EXIT_NOT_APPLIED,
    FEED_URL_ENV,
    UPDATE_ENDPOINT,
    UpdateCheckOut,
)
from apps.engine_core.update_channel_engine_client import (
    ApplyOutcome,
    apply_via_engine,
    check_via_engine,
)
from apps.engine_core.update_channel_feed import (
    resolve_update_check,
)
from apps.shared import platform_paths


# ----- CLI ----------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """``python -m apps.engine_core.update_channel check``.

    The agent-native half of the UI's check button: same resolver, same
    endpoint, JSON on stdout, and a NON-ZERO exit when the channel could not
    be read so a script cannot mistake an outage for "up to date".
    """
    parser = argparse.ArgumentParser(
        prog="python -m apps.engine_core.update_channel",
        description="Ask the Open DJ update channel what it is offering.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="compare this build against the channel")
    check.add_argument(
        "--endpoint",
        default=None,
        help=(
            f"feed URL to read (default: {UPDATE_ENDPOINT}, or "
            f"${FEED_URL_ENV}/{CHECKED_FEED_FILE} when set). A '.yml' URL is "
            "read as an electron-updater feed, a '.json' URL as a Tauri latest.json"
        ),
    )
    check.add_argument(
        "--engine",
        default=None,
        help=(
            "origin of a RUNNING engine to ask instead of resolving locally, "
            "e.g. http://127.0.0.1:8685. Use this to check the build a "
            "packaged app is actually running."
        ),
    )
    apply = sub.add_parser(
        "apply",
        help="request install-and-restart on a running packaged engine",
    )
    apply.add_argument(
        "--engine",
        required=True,
        help="origin of the RUNNING engine, e.g. http://127.0.0.1:8685",
    )
    apply.add_argument(
        "--timeout-s",
        type=float,
        default=APPLY_TIMEOUT_S,
        help=(f"seconds to wait for the shell to install and the app to relaunch (default: {APPLY_TIMEOUT_S:g})"),
    )
    args = parser.parse_args(argv)

    if args.command == "check":
        if args.engine is not None:
            return _check_via_engine(args.engine)
        return _check_locally(args.endpoint)
    if args.command == "apply":
        return _apply_via_engine(args.engine, args.timeout_s)
    raise AssertionError(f"unknown command: {args.command}")


def _check_locally(endpoint: str | None) -> int:
    try:
        info = resolve_build_info(dict(os.environ), platform_paths.PROJECT_ROOT)
        identity = BuildIdentity(info=info, failure=None)
    except BuildInfoUnavailable as exc:
        identity = BuildIdentity(info=None, failure=str(exc))
    with httpx.Client() as client:
        result = resolve_update_check(identity, client=client, endpoint=endpoint)
    return _emit(result)


def _check_via_engine(origin: str) -> int:
    """Print the running engine's answer, and exit 0 only for an actionable one."""
    outcome = check_via_engine(origin)
    print(json.dumps(outcome.document, indent=2))
    if not outcome.actionable:
        print(f"[ERROR] {outcome.status}: {outcome.detail}", file=sys.stderr)
        return EXIT_NOT_APPLIED
    return EXIT_APPLIED


def _emit(result: UpdateCheckOut) -> int:
    print(json.dumps(result.model_dump(), indent=2))
    if result.status not in ANSWERED:
        print(f"[ERROR] {result.status}: {result.detail}", file=sys.stderr)
        return 2
    if result.status == "update-available":
        print(
            f"[UPDATE] {result.current_version} -> {result.available_version}",
            file=sys.stderr,
        )
    return 0


def _apply_via_engine(origin: str, timeout_s: float = APPLY_TIMEOUT_S) -> int:
    """Print what one apply found, and return its exit code.

    A renderer over :func:`apply_via_engine`, not a second implementation:
    the packaged CLI prints the SAME outcome the same way, so the two cannot
    disagree about whether an install landed.
    """
    outcome = apply_via_engine(origin, timeout_s, progress=_stderr_progress)
    _emit_apply(outcome)
    return outcome.code


def _stderr_progress(message: str) -> None:
    print(f"[APPLY] {message}", file=sys.stderr)


def _emit_apply(outcome: ApplyOutcome) -> None:
    if outcome.check:
        print(json.dumps(outcome.check, indent=2))
    if outcome.shell_status is not None:
        print(json.dumps(outcome.shell_status, indent=2))
    if outcome.before or outcome.after:
        print(
            json.dumps(
                {
                    "before": outcome.before,
                    "after": outcome.after,
                    "changed": outcome.applied,
                    "reason": outcome.reason,
                },
                indent=2,
            )
        )
    if outcome.code != EXIT_APPLIED:
        print(f"[ERROR] {outcome.reason}: {outcome.detail}", file=sys.stderr)
