"""``opendj mcp``: stdio MCP entry for installed Open DJ."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli.client import ORDER_TIMEOUT_S
from apps.opendj_cli.mcp_safety import ENABLE_DESTRUCTIVE_ENV


def run(
    rest: Sequence[str],
    *,
    lock: Path | None = None,
    command_timeout_s: float = ORDER_TIMEOUT_S,
) -> int:
    parser = argparse.ArgumentParser(prog="opendj mcp")
    parser.add_argument(
        "--enable-destructive",
        action="store_true",
        help="allow destructive library verbs in this process (tests only)",
    )
    args = parser.parse_args(list(rest))
    if args.enable_destructive:
        os.environ[ENABLE_DESTRUCTIVE_ENV] = "1"
    from apps.opendj_cli.mcp_server import run_stdio

    run_stdio(lock_path=lock, command_timeout_s=command_timeout_s)
    return 0
