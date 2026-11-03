"""Shared playlist-aware CLI entry point for stems and vocals.

This lives in ``apps.vocals`` because it catches ``UnknownPlaylistError``;
``apps.shared`` must not import from the vocals package.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from apps.vocals.errors import UnknownPlaylistError


def run_playlist_aware_cli(
    build_parser: Callable[[], object],
    *,
    argv: Sequence[str] | list[str] | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except UnknownPlaylistError as exc:
        raise SystemExit(
            2,
            f"error: unknown playlist {exc.name!r}. "
            f"Known: {', '.join(exc.known) or '(none)'}",
        ) from exc
