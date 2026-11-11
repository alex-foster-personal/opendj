"""REQHYG-03: disposition hooks for harvested prompts (issue #11334: pytest TEST run gate)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

from scripts.prompt_targets import target_test_problem
from scripts.reqs_registry import requirement_ids

DISPOSITIONS = frozenset({"REQ", "TEST", "NOTE"})
EXIT_REFUSED = 2


def _refuse(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(EXIT_REFUSED)


def _validated_target(checkout: Path, disposition: str, target: str | None) -> str:
    """The target to store, validated against `checkout`, or exit 2 / 3."""
    if disposition not in DISPOSITIONS:
        _refuse(f"unknown disposition: {disposition!r}")
    if disposition == "NOTE":
        if target:
            _refuse("NOTE must not carry a target")
        return ""
    if not target:
        kind = "a requirement id" if disposition == "REQ" else "a test id"
        _refuse(f"{disposition} requires {kind} target")

    def known() -> frozenset[str]:
        try:
            return requirement_ids(checkout)
        except (FileNotFoundError, ValueError) as exc:
            print(f"UNAVAILABLE: {exc}", file=sys.stderr)
            sys.exit(EXIT_UNAVAILABLE)

    if disposition == "REQ":
        if target not in known():
            _refuse(f"unknown requirement id: {target} (checked {checkout / 'reqs.json'})")
    elif problem := target_test_problem(checkout, target, known):
        _refuse(problem)
    return target
