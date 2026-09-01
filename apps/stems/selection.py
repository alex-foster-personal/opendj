"""Which tracks still need stems, counted against an HONEST denominator.

One function, three readers: the plan endpoint that fills the install-flow
prompt, the job kind that turns ``scope: "pending"`` into a run, and the
worker that resolves that scope for real. They MUST agree -- a prompt that
says 412 tracks and a run that separates 380 is the kind of quiet
disagreement nobody notices until the invoice.

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

# ----- CFG -------------------------------------------------------------------
MANIFEST_NAME: str = "manifest.json"


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
    "has_bundle",
    "library_buckets",
]
