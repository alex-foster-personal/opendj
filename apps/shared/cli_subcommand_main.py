"""Dispatch pattern for argparse subcommand CLIs."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence


def run_subcommand_cli(
    parser: argparse.ArgumentParser,
    commands: dict[str, Callable[[argparse.Namespace], None]],
    *,
    argv: Sequence[str] | None = None,
    error_type: type[BaseException],
) -> int:
    args = parser.parse_args(argv)
    try:
        commands[args.command](args)
    except error_type as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0
