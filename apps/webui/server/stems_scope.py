"""The engine's stems scope: the enrich card's "not yet" songs, as a job target (STEM-58).

Wed 7 Oct 2026, silver: the card said "Stems: 1,042 separated, 3 not yet",
the maintainer pressed Separate, and a ``stems.separate`` job with ``scope: pending``
ran 7.5 s, reported "done", and left the card at 3. The card counted SONGS
over ``present`` (playability-resolved audio, bundles validated across every
stem root, R2-held bundles counted done), while the worker resolved its own
scope with ``apps.stems.selection.library_buckets`` (ROWS, raw ``file_path``,
one stem root, a manifest probe). Only 17 of silver's 11,164 rows exist at
their recorded ``file_path``; the card finds the rest through this Mac's
``track_locations`` rows. So the job filed the card's songs under "no audio
on disk", worked (at most) on rows of its own choosing, exited 0, and the
runner called exit 0 "done" without looking at a single bundle.

This module is the one definition both now read: the coverage snapshot the
card is built from (``routes.ingest.build_snapshot``), folded to
:class:`apps.stems.selection.StemsScope`. ``app_wiring`` puts
:func:`pending_stems_scope` on ``app.state.stems_scope_fn`` so the jobs API
and the plan endpoint, which may not import this package, read it from there.

Requirements (mini-PRD):
  ✔︎ the job's pending count IS the card's not-yet count
    [if] the card says N songs not yet [then] the scope holds N representatives
    [if] two rows are one song [then] the scope holds one of them
    [if] a song's only copy has a corrupt bundle [then] it is pending, never ready

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI

from apps.stems.selection import Candidate, StemsScope
from apps.webui.server.routes import ingest as ingest_routes
from apps.webui.server.routes.ingest_coverage import CoverageSnapshot

ID_BIND_BATCH: int = 500


def _durations_ms(stable_ids: Sequence[str]) -> dict[str, int]:
    """Track lengths for the plan's time and cost estimate, from ``tracks``."""
    out: dict[str, int] = {}
    conn: sqlite3.Connection = ingest_routes.open_ro()
    try:
        ids = list(stable_ids)
        for start in range(0, len(ids), ID_BIND_BATCH):
            batch = ids[start : start + ID_BIND_BATCH]
            marks = ",".join("?" * len(batch))
            for sid, duration_ms in conn.execute(
                f"SELECT stable_id, duration_ms FROM tracks WHERE stable_id IN ({marks}) AND deleted_at IS NULL",
                batch,
            ):
                out[str(sid)] = int(duration_ms or 0)
    finally:
        conn.close()
    return out


def scope_from_snapshot(snapshot: CoverageSnapshot) -> StemsScope:
    """The snapshot's stems step over songs, plus the representatives to separate."""
    if snapshot.songs is None:
        raise RuntimeError("coverage snapshot carries no song view; the stems scope needs one")
    steps = snapshot.songs["steps"]["stems"]
    durations = _durations_ms([sid for sid, _path in snapshot.stems_pending_songs])
    return StemsScope(
        pending=tuple(
            Candidate(stable_id=sid, audio_path=Path(path), duration_ms=durations.get(sid, 0))
            for sid, path in snapshot.stems_pending_songs
        ),
        songs=int(snapshot.songs["songs"]),
        done=int(steps["done"]),
        terminal=int(steps["terminal"]),
        failed=int(steps["failed"]),
        rows_present=len(snapshot.on_disk),
        rows_unreachable=int(snapshot.unreachable),
    )


def pending_stems_scope(app: FastAPI) -> StemsScope:
    """A fresh measurement, never the cached one: a job is about to act on it."""
    return scope_from_snapshot(ingest_routes.build_snapshot(app))


__all__ = ["pending_stems_scope", "scope_from_snapshot"]
