"""``opendj rec``: drive the running app's REC over HTTP (SETREC-11802).

The same routes as ``python -m apps.sets rec`` and the REC picker.

    opendj rec start --source master
    opendj rec status
    opendj rec stop
    opendj rec recover
    opendj rec remembered-input  (the last REC input choice, from the daemon)
    opendj rec transitions SESSION_ID  (SET-12826; HTTP twin of GET /api/sets/{id}/transitions)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE, api_cli
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail
from apps.sets import rec_cli as sets_rec_cli
from apps.sets.recorder_service import RECORD_SOURCES

_USAGE_DISPATCH = (
    "usage: opendj rec start --source master|loopback|external|none [--device-name NAME] | status | stop | recover"
)
_USAGE_RECOVER = "usage: opendj rec recover"
_USAGE_TRANSITIONS = "usage: opendj rec transitions SESSION_ID"
_USAGE_REMEMBERED = "usage: opendj rec remembered-input"


def _namespace(rest: Sequence[str], base_url: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="opendj rec")
    sub = parser.add_subparsers(dest="rec_cmd", required=True)
    for name in ("start", "status", "stop"):
        cmd = sub.add_parser(name)
        if name == "start":
            cmd.add_argument("--source", required=True, choices=RECORD_SOURCES)
            cmd.add_argument("--device-name", default=None)
            cmd.add_argument(
                "--sources",
                nargs="*",
                default=["djay_monitor", "opendj_decks"],
            )
    parsed, unknown = parser.parse_known_args(rest)
    if unknown:
        raise ValueError(f"unrecognized arguments: {' '.join(unknown)}")
    return argparse.Namespace(
        base_url=base_url,
        rec_cmd=parsed.rec_cmd,
        source=getattr(parsed, "source", None),
        device_name=getattr(parsed, "device_name", None),
        sources=getattr(parsed, "sources", ["djay_monitor", "opendj_decks"]),
        session_id=None,
    )


def _run_recover(rest: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    """Recover over HTTP with the same status preflight as ``python -m apps.sets rec``."""
    if list(rest) in (["--help"], ["-h"], ["recover", "--help"], ["recover", "-h"]):
        print(_USAGE_RECOVER)
        print(_USAGE_TRANSITIONS)
        return EXIT_CONFIRMED
    if list(rest) != ["recover"]:
        return _fail(as_json, "usage", _USAGE_RECOVER, EXIT_FAILED)
    try:
        target = resolve_backend_base_url(lock_path=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        status = sets_rec_cli.recover(target.base_url)
    except sets_rec_cli.RecCommandFailed as error:
        return _fail(as_json, "recorder_recovery_failed", str(error), EXIT_FAILED)
    print(json.dumps(status, indent=2, sort_keys=True))
    return EXIT_CONFIRMED


def _run_transitions(rest: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    """Print the session transitions JSON from the running app; it does not re-classify on disk."""
    if len(rest) != 2 or not rest[1]:
        return _fail(as_json, "usage", _USAGE_TRANSITIONS, EXIT_FAILED)
    try:
        target = resolve_backend_base_url(lock_path=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    ns = argparse.Namespace(rec_cmd="transitions", base_url=target.base_url, session_id=rest[1])
    try:
        code = sets_rec_cli.dispatch(ns)
    except sets_rec_cli.RecCommandFailed as error:
        return _fail(as_json, "rec_failed", str(error), EXIT_FAILED)
    if code != 0:
        return EXIT_FAILED if not as_json else _fail(as_json, "rec_failed", "rec transitions failed", EXIT_FAILED)
    return EXIT_CONFIRMED


def _run_remembered_input(rest: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    """Print the daemon's remembered-input document, preserving errors and exit codes."""
    if list(rest) != ["remembered-input"]:
        return _fail(as_json, "usage", _USAGE_REMEMBERED, EXIT_FAILED)
    return api_cli.run(["GET", "/api/sets/recorder/remembered-input"], as_json=as_json, lock=lock)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if list(rest) in (["--help"], ["-h"]):
        print(_USAGE_DISPATCH)
        print(_USAGE_RECOVER)
        return EXIT_CONFIRMED
    if not rest:
        return _fail(as_json, "usage", _USAGE_DISPATCH, EXIT_FAILED)
    if rest[0] == "recover":
        return _run_recover(rest, as_json=as_json, lock=lock)
    if rest[0] == "transitions":
        return _run_transitions(rest, as_json=as_json, lock=lock)
    if rest[0] == "remembered-input":
        return _run_remembered_input(rest, as_json=as_json, lock=lock)
    if rest[0] not in ("start", "status", "stop"):
        return _fail(as_json, "usage", _USAGE_DISPATCH, EXIT_FAILED)
    return _run_dispatch(rest, as_json=as_json, lock=lock)


def _run_dispatch(rest: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    """Parse start/status/stop before discovery, then delegate to ``apps.sets`` rec dispatch."""
    try:
        args = _namespace(rest, "")
    except (ValueError, argparse.ArgumentError) as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    except SystemExit:
        return _fail(as_json, "usage", _USAGE_DISPATCH, EXIT_FAILED)
    try:
        args.base_url = resolve_backend_base_url(lock_path=lock).base_url
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        code = sets_rec_cli.dispatch(args)
    except sets_rec_cli.RecCommandFailed as error:
        return _fail(as_json, "rec_failed", str(error), EXIT_FAILED)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
