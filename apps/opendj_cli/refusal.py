"""Shared JSON/text refusal envelope for ``opendj`` CLI verbs."""

from __future__ import annotations

import json
import sys


def fail(as_json: bool, code: str, message: str, exit_code: int) -> int:
    """Print a refusal and return its exit code.

    JSON mode writes ``{"error": {"code", "message"}, "exit_code"}`` to stdout
    (indent=2, sort_keys=True). Text mode writes ``opendj: {message}`` to stderr.
    The envelope is the same one every installed-app verb already emits, so an
    agent parsing one verb can parse them all.
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
