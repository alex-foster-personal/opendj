"""Shared argv dispatch for ``python -m apps.<pkg>`` CLIs with subcommands."""

from __future__ import annotations

import sys
from collections.abc import Callable

Handler = Callable[[list[str]], int]


def dispatch_subcommand_main(
    commands: dict[str, tuple[Handler, str]],
    *,
    usage: str,
    argv: list[str] | None = None,
) -> int:
    """Run ``commands[name][0](rest)`` or print usage / exit 2 on unknown name."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(usage)
        return 0
    name, rest = args[0], args[1:]
    entry = commands.get(name)
    if entry is None:
        print(f"unknown subcommand {name!r}\n", file=sys.stderr)
        print(usage, file=sys.stderr)
        return 2
    return entry[0](rest)
