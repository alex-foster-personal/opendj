"""Refuse `python -m apps.lyrics.cli_<x>`: those modules are not entrypoints.

`cli_pipeline`, `cli_storage` and `cli_verdicts` only register subcommands on
the one parser `python -m apps.lyrics` builds. Run directly they used to import
and exit 0 having done nothing, which a caller reads as success (Sat 26 Sep
2026 retest: `python -m apps.lyrics.cli_pipeline jobs work --once` printed
nothing and exited 0, and so did a bogus flag through the same module).
"""

from __future__ import annotations

import sys

ENTRYPOINT = "python -m apps.lyrics"
EXIT_USAGE = 2


def refuse_direct_run(module: str) -> None:
    """Exit with a usage error naming the real entrypoint and the same argv."""
    argv = " ".join(sys.argv[1:])
    print(
        f"error: {module} is not an entrypoint; it only registers subcommands. Run: {ENTRYPOINT} {argv}".rstrip(),
        file=sys.stderr,
    )
    raise SystemExit(EXIT_USAGE)


__all__ = ["ENTRYPOINT", "EXIT_USAGE", "refuse_direct_run"]
