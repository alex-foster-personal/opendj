"""``opendj replay takeover <control|all>`` (SETREC-12067): replay take-over on the performance page."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE, EXIT_NO_PAGE
from apps.opendj_cli.client import EngineClient, NoPerformancePage, OrderRejected
from apps.opendj_cli.origin import EngineNotRunning, resolve_verified_origin
from apps.opendj_cli.refusal import fail as _fail

CONTROL_RE = re.compile(r"^(all|deck\.[1-4](\.[a-z0-9_]+)+|mixer\.[a-z_]+)$")
USAGE = "usage: opendj replay takeover <control|all>"


def takeover_order(control: str) -> dict[str, Any]:
    if not CONTROL_RE.fullmatch(control):
        raise ValueError(f"invalid control id {control!r}; expected dotted id or all")
    return {"single": {"type": "replay_takeover", "control": control}}


def _first_step(result: dict[str, Any]) -> dict[str, Any] | None:
    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    first = steps[0]
    return first if isinstance(first, dict) else None


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opendj replay")
    sub = parser.add_subparsers(dest="verb", required=True)
    takeover = sub.add_parser("takeover", help="take over one control or all during replay")
    takeover.add_argument("control", help="dotted control id or all, e.g. deck.1.eq.low")
    try:
        ns = parser.parse_args(list(rest))
    except SystemExit:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    if ns.verb != "takeover":
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    control = str(ns.control)
    try:
        order = takeover_order(control)
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    try:
        client = EngineClient(origin=resolve_verified_origin(lock))
        result = client.post_order(order)
    except EngineNotRunning as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except NoPerformancePage as error:
        return _fail(as_json, "no_performance_page", str(error), EXIT_NO_PAGE)
    except OrderRejected as error:
        return _fail(as_json, "order_rejected", str(error), EXIT_FAILED)
    step = _first_step(result)
    if step is not None and step.get("status") == "failed":
        return _fail(as_json, "order_failed", str(step.get("error", "replay takeover failed")), EXIT_FAILED)
    payload = step.get("result") if step is not None else None
    if as_json:
        print(json.dumps(payload or result, indent=2, sort_keys=True))
    else:
        taken = control
        if isinstance(payload, dict) and payload.get("taken_over") is not None:
            taken = str(payload["taken_over"])
        print(f"taken over: {taken}")
    return EXIT_CONFIRMED


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
