"""Rebind closures shared by load and backfill ``still_valid`` checks.

Both :mod:`apps.mik.cli_load` and :mod:`apps.mik.backfill` must re-run their
own matching against a locked state connection when a
:class:`~apps.mik.load_types.LoadPlan` asks whether bindings changed after
planning. The closure shape is fixed by :data:`apps.mik.duplicates.Rebind`; this
module builds it once so the two call sites stay aligned.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from . import duplicates as dups
from . import match as matcher
from . import sources_match as sources
from .match import MatchReport
from .mikdb import MikSong


def plan_rebind(
    songs: list[MikSong],
    *,
    sources_batch: Path | None = None,
    library_root: Path | None = None,
    allow_fuzzy: bool = True,
) -> dups.Rebind:
    """Return the rebind closure ``binding_unchanged`` uses for this run."""

    def rebind(
        locked: sqlite3.Connection,
    ) -> tuple[list[MikSong], MatchReport, Callable[[MikSong], list[str]]]:
        if sources_batch is not None:
            batch = sources.match_songs_by_sources(
                songs,
                locked,
                batch_dir=sources_batch,
                library_root=library_root,
                platform=sources.HOST_PLATFORM,
            )
            return batch.songs, batch.report, batch.candidates_of
        index = matcher.TrackIndex.from_conn(locked)
        return (
            songs,
            matcher.match_songs(songs, index, allow_fuzzy=allow_fuzzy),
            dups.tier_candidates(index, allow_fuzzy=allow_fuzzy),
        )

    return rebind


__all__ = ["plan_rebind"]
