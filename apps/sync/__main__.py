"""``python -m apps.sync <subcommand> [args]`` -- rb-djay sync front door.

HTTP twins live under ``/api/v1/rb-djay-sync/*``. Each subcommand forwards
argv to the existing module ``main()`` with lazy imports so fingerprint and
pyrekordbox costs stay opt-in.

    python -m apps.sync
    python -m apps.sync match --no-fingerprint
    python -m apps.sync playlist-diff --help
    python -m apps.sync metadata-diff --include-cues
"""
from __future__ import annotations

import sys
from collections.abc import Callable

Handler = Callable[[list[str]], int]


def _match(argv: list[str]) -> int:
    from apps.audit import match_rb_djay

    return match_rb_djay.main(argv)


def _playlist_diff(argv: list[str]) -> int:
    from apps.sync import playlist_diff

    return playlist_diff.main(argv)


def _playlist_apply(argv: list[str]) -> int:
    from apps.sync import playlist_apply

    return playlist_apply.main(argv)


def _metadata_diff(argv: list[str]) -> int:
    from apps.audit import sync_diff

    return sync_diff.main(argv)


def _cue_diff(argv: list[str]) -> int:
    from apps.audit import cue_comparison

    return cue_comparison.main(argv)


def _apply_cues(argv: list[str]) -> int:
    from apps.sync import apply_cues

    return apply_cues.main(argv)


def _apply_analysis(argv: list[str]) -> int:
    from apps.sync import apply_analysis

    return apply_analysis.main(argv)


def _apply_ratings(argv: list[str]) -> int:
    from apps.sync import apply_ratings

    return apply_ratings.main(argv)


def _djay_playlists(argv: list[str]) -> int:
    from apps.sync import djay_playlists_cli

    return djay_playlists_cli.main(argv)


COMMANDS: dict[str, tuple[Handler, str]] = {
    "match": (_match, "build matches.csv (SYNC-02)"),
    "playlist-diff": (_playlist_diff, "compute playlist-plan.json (SYNC-03)"),
    "playlist-apply": (_playlist_apply, "apply playlist plan to djay (SYNC-03)"),
    "metadata-diff": (_metadata_diff, "analysis + ratings diff CSVs (SYNC-05/06)"),
    "cue-diff": (_cue_diff, "cue comparison CSV (SYNC-04)"),
    "apply-cues": (_apply_cues, "apply cue diff (SYNC-04, dry-run default)"),
    "apply-analysis": (_apply_analysis, "apply analysis diff (SYNC-05)"),
    "apply-ratings": (_apply_ratings, "apply ratings diff (SYNC-06)"),
    "djay-playlists": (_djay_playlists, "list djay playlists + memberships (SYNC-01)"),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        "usage: python -m apps.sync <subcommand> [args]",
        "",
        "HTTP twins: GET/POST /api/v1/rb-djay-sync/*",
        "",
        "subcommands:",
    ]
    lines += [f"  {name:<{width}}  {help_}" for name, (_, help_) in COMMANDS.items()]
    lines += ["", "Run `python -m apps.sync <subcommand> --help` for its flags."]
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
