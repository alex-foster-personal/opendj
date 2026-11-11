"""Which tracks still need stems, counted against an HONEST denominator.

TWO DEFINITIONS LIVE HERE, and the engine uses only one of them (STEM-58).

* :class:`StemsScope` is the engine's: songs over ``present``, the same
  measurement the enrich card shows. The plan endpoint, the card and a
  ``stems.separate`` job's ``scope: pending`` all read it, so they agree. The
  engine resolves the scope to explicit ids AT ENQUEUE, so a worker spawned by
  the engine never receives ``--scope``.
* :func:`library_buckets` is the offline CLI's: rows, raw ``file_path``, one
  stem root, a manifest probe. It answered the engine too until Wed 7 Oct
  2026, when the card said "3 not yet" on silver and a scope job ran 7.5 s and
  reported "done": only 17 of silver's 11,164 rows exist at their recorded
  ``file_path`` (the rest resolve through this Mac's location rows), so this
  function filed the card's songs under "no audio on disk". It remains only
  for ``--scope pending`` on the command line, where no engine (and so no
  coverage snapshot) exists.

THE DENOMINATOR RULE (CLAUDE.md). "How many tracks need stems" is
meaningless without saying which rows were even eligible. A library row can
be in exactly one of three buckets:

  ready        a bundle is already on disk for it
  pending      audio is on disk, no bundle yet -- the real work
  unavailable  the library row exists but its file does not

``unavailable`` is reported, never silently dropped, because a tester whose
volume is unmounted must see "no audio for 900 of these" rather than a
smaller job than they expected and no explanation.

Requirements (mini-PRD):
  ✔︎ ✅ every library row with a file path lands in exactly one bucket.
    [if] a row's file is missing [then] unavailable, never pending
    [if] a row has a manifest [then] ready, never pending
  ✔︎ ✅ counting the whole library never opens a stem file.
    [if] the library has 8000 rows [then] the plan is a directory probe per
      row, not a FLAC decode per row

-Claude
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from apps.stems.constants import STEM_MANIFEST_FILENAME

# ----- CFG -------------------------------------------------------------------
MANIFEST_NAME: str = STEM_MANIFEST_FILENAME


@dataclass(frozen=True)
class Candidate:
    stable_id: str
    audio_path: Path
    duration_ms: int


@dataclass(frozen=True)
class LibraryBuckets:
    """Every library row with a file path, partitioned. Never overlapping."""

    pending: list[Candidate] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.pending) + len(self.ready) + len(self.unavailable)

    def pending_ids(self) -> list[str]:
        return [candidate.stable_id for candidate in self.pending]

    def pending_durations_s(self) -> list[float]:
        return [candidate.duration_ms / 1000.0 for candidate in self.pending]


@dataclass(frozen=True)
class StemsScope:
    """What "stems not yet" means to the engine: SONGS, over ``present`` (STEM-58).

    The enrich card's line, the ``GET /stems/plan`` prompt and a
    ``stems.separate`` job with ``scope: pending`` all read this one value.
    Built by the composition root from the coverage snapshot
    (``apps.webui.server.stems_scope``), because the definition needs the
    playability scan, bundle validation over every stem root, the R2 index and
    the song index -- none of which this domain package may import.

    ``pending`` holds ONE representative row per pending song, so separating
    every entry turns every "not yet" song into a done one.
    """

    pending: tuple[Candidate, ...]
    songs: int
    done: int
    terminal: int
    failed: int
    rows_present: int
    rows_unreachable: int

    def __post_init__(self) -> None:
        counted = len(self.pending) + self.done + self.terminal + self.failed
        if counted != self.songs:
            raise ValueError(
                f"stems scope does not partition its songs: {len(self.pending)} pending "
                f"+ {self.done} done + {self.terminal} terminal + {self.failed} failed "
                f"!= {self.songs} songs"
            )

    def pending_ids(self) -> list[str]:
        return [candidate.stable_id for candidate in self.pending]

    def pending_durations_s(self) -> list[float]:
        return [candidate.duration_ms / 1000.0 for candidate in self.pending]


def has_bundle(stable_id: str, stems_root: Path) -> bool:
    """Cheap presence probe: a bundle directory carrying a manifest.

    Deliberately NOT ``load_stem_bundle``. Full validation decodes four FLAC
    headers per track, which is the right thing to do when a deck is about to
    PLAY a bundle and the wrong thing to do when counting 8000 of them for a
    prompt. A corrupt bundle counted here as ready still refuses at play time,
    where the reader validates -- so the cheap count can overstate readiness
    but can never produce a silent wrong playback.
    """
    return (stems_root / stable_id / MANIFEST_NAME).is_file()


def library_buckets(data_dir: Path, stems_root: Path | None = None) -> LibraryBuckets:
    """Partition every library row with a file path into the three buckets."""
    state_db = data_dir / "state" / "state.db"
    if not state_db.is_file():
        raise FileNotFoundError(f"state.db missing: {state_db}")
    root = stems_root if stems_root is not None else data_dir / "state" / "stems"

    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT stable_id, file_path, duration_ms FROM tracks "
            "WHERE file_path IS NOT NULL AND file_path != '' "
            "AND deleted_at IS NULL "
            "ORDER BY stable_id"
        ).fetchall()
    finally:
        connection.close()

    buckets = LibraryBuckets()
    for stable_id, file_path, duration_ms in rows:
        if has_bundle(stable_id, root):
            buckets.ready.append(stable_id)
            continue
        if not Path(file_path).is_file():
            buckets.unavailable.append(stable_id)
            continue
        buckets.pending.append(
            Candidate(
                stable_id=stable_id,
                audio_path=Path(file_path),
                duration_ms=int(duration_ms or 0),
            )
        )
    return buckets


__all__ = [
    "Candidate",
    "LibraryBuckets",
    "StemsScope",
    "has_bundle",
    "library_buckets",
]
