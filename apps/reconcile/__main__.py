"""``python -m apps.reconcile <subcommand> [args]`` -- reconcile front door.

Every reconcile module already owns a complete argparse of its own (the repo's
house shape: one module, one ``main(argv)``). This dispatcher is deliberately
thin -- it maps a subcommand name to that module's ``main`` and forwards the
remaining argv untouched, so ``python -m apps.reconcile relink --help`` prints
the relink parser's own help and ``python -m apps.reconcile.relink`` keeps
working unchanged. No flags are re-declared here; there is nothing to drift.

    python -m apps.reconcile                      # list subcommands
    python -m apps.reconcile list-broken --help
    python -m apps.reconcile relink               # dry-run (default)
    python -m apps.reconcile relink --live --i-understand-the-risks
    python -m apps.reconcile relink-undo --log <log.csv>
    python -m apps.reconcile relink-review
"""
from __future__ import annotations

import sys
from collections.abc import Callable

Handler = Callable[[list[str]], int]


def _index_disk(argv: list[str]) -> int:
    from apps.reconcile import index_disk

    return index_disk.main(argv)


def _match(argv: list[str]) -> int:
    from apps.reconcile import match

    return match.main(argv)


def _relink(argv: list[str]) -> int:
    from apps.reconcile import relink

    return relink.cmd_relink(argv)


def _relink_undo(argv: list[str]) -> int:
    from apps.reconcile import relink

    return relink.cmd_relink_undo(argv)


def _relink_review(argv: list[str]) -> int:
    from apps.reconcile import tiebreak

    return tiebreak.main(argv)


def _no_arg(name: str, fn: Callable[[], object]) -> Handler:
    """Adapt a legacy ``main() -> None`` module to the dispatcher contract.

    ``list_broken`` and ``locate`` predate argparse in this package and take no
    arguments at all. Silently swallowing flags a caller passed would be the
    worst outcome, so an unexpected argument is an error.
    """

    def handler(argv: list[str]) -> int:
        if argv:
            print(
                f"{name} takes no arguments (got {argv}); it is configured by "
                "module constants, see apps/reconcile/{name}.py",
                file=sys.stderr,
            )
            return 2
        fn()
        return 0

    return handler


def _reacquire(argv: list[str]) -> int:
    from apps.reconcile import reacquire

    return reacquire.main(argv)


def _list_broken(argv: list[str]) -> int:
    from apps.reconcile import list_broken

    return _no_arg("list-broken", list_broken.main)(argv)


def _locate(argv: list[str]) -> int:
    from apps.reconcile import locate

    return _no_arg("locate", locate.main)(argv)


def _apply(argv: list[str]) -> int:
    from apps.reconcile import apply

    return apply.main(argv)


# Imports are lazy inside each handler on purpose: ``relink`` must not pay for
# pyrekordbox (pulled in by ``apply``) and ``list-broken`` must not pay for
# the tag reader. Handlers, not module objects, keep that laziness explicit.
COMMANDS: dict[str, tuple[Handler, str]] = {
    "index-disk": (_index_disk, "walk audio roots, cache tags (read-only)"),
    "match": (_match, "score relink candidates, classify every row (read-only)"),
    "relink": (_relink, "apply tracks.file_path relinks (dry-run by default)"),
    "relink-undo": (_relink_undo, "revert a relink run from its reversal log"),
    "relink-review": (_relink_review, "rank ambiguous candidates for a human"),
    "reacquire": (_reacquire, "ranked re-acquisition worklist for absent-no-audio"),
    "list-broken": (_list_broken, "list rekordbox rows whose file is missing"),
    "locate": (_locate, "search the disk for a broken rekordbox row"),
    "apply": (_apply, "write located paths into rekordbox master.db"),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = ["usage: python -m apps.reconcile <subcommand> [args]", "", "subcommands:"]
    lines += [f"  {name:<{width}}  {help_}" for name, (_, help_) in COMMANDS.items()]
    lines += ["", "Run `python -m apps.reconcile <subcommand> --help` for its flags."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_usage())
        return 0
    name, rest = args[0], args[1:]
    entry = COMMANDS.get(name)
    if entry is None:
        print(f"unknown subcommand {name!r}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    return entry[0](rest)


if __name__ == "__main__":
    raise SystemExit(main())
