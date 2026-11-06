"""JSON plan documents for the AutoDJ CLIs: a path or stdin, decoded as an object.

Private to this package. Mix scores stay on the ``opendj dj`` path; pair-transition
reads JSON only. ``RecursionError`` from ``json.loads`` is left for each CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class PlanIoError(Exception):
    """Unreadable plan path or stdin JSON; each CLI maps this to usage."""


def parse_plan_json(text: str, source: str) -> dict[str, Any]:
    """Decode plan JSON text. ``source`` is the path or '-' used in error messages.

    ``RecursionError`` from ``json.loads`` is not caught: ``dj_cli`` maps it to
    ``plan_invalid``; pair-transition leaves it uncaught, as today.
    """
    try:
        document = json.loads(text)
    except ValueError as error:
        raise PlanIoError(f"plan {source} is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise PlanIoError(f"plan {source} must be a JSON object")
    return document


def read_plan_json(source: str) -> dict[str, Any]:
    """Read a plan JSON object from a path, or '-' for stdin. Mix scores are not accepted."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as error:
        raise PlanIoError(f"cannot read plan {source}: {error}") from error
    return parse_plan_json(text, source)
