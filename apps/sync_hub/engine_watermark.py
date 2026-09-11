"""Machine-local sync position: watermarks against a peer, changelog sequences.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4). Nothing here is synced -- ``sync_state`` is machine-local
bookkeeping about THIS machine's progress against a peer, never a row this
module offers anyone.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub.engine_common import HUB_CHANGELOG_TABLE

# ----- watermarks (machine-local ``sync_state``) ---------------------------


@dataclass(frozen=True)
class Watermark:
    """This machine's position against one peer. Machine-local, never synced.

    * ``last_push_seq`` is the greatest ``local_changelog.seq`` this machine
      has already offered to ``peer``. The push fence (ADR 08 point 3).
    * ``last_pull_seq`` is the greatest ``hub_changelog.seq`` already applied
      from ``peer``.
    * ``last_sync_at`` records that a sync against ``peer`` has completed at
      least once, and when. It is an operator readout and the "already
      seeded" flag behind :attr:`needs_full_offer` -- deliberately NOT a
      watermark. Round 1 lost rows precisely because a wall clock was one
      (finding 3), so nothing here compares it against a row's
      ``updated_at``.
    * ``peer_generation`` is the token ``peer`` reported at the last
      completed sync (:mod:`apps.sync_hub.generation`). A different token
      next time means that peer's DB moved backwards and everything above is
      meaningless, which is round 2's replacement for inferring a restore
      from the peer's ``MAX(seq)`` (finding N6). None until one sync
      completes.
    """

    peer: str
    last_push_seq: int = 0
    last_pull_seq: int = 0
    last_sync_at: str | None = None
    peer_generation: str | None = None

    @property
    def needs_full_offer(self) -> bool:
        """True until one sync against this peer has completed.

        Rows that predate ``local_changelog`` (anything migrated in from v5,
        which on a real library is the whole library) have no changelog entry
        to fence against, so the first offer has to be a full scan. It is
        also what a hub-restore reset falls back to (ADR 08 point 4).
        """
        return self.last_sync_at is None


def read_watermark(conn: sqlite3.Connection, peer: str) -> Watermark:
    """Load the ``sync_state`` row for ``peer``; zeros if never synced."""
    row = conn.execute(
        "SELECT last_push_seq, last_pull_seq, last_sync_at, peer_generation "
        "FROM sync_state WHERE peer = ?",
        (peer,),
    ).fetchone()
    if row is None:
        return Watermark(peer=peer)
    return Watermark(
        peer=peer,
        last_push_seq=int(row[0]),
        last_pull_seq=int(row[1]),
        last_sync_at=None if row[2] is None else str(row[2]),
        peer_generation=None if row[3] is None else str(row[3]),
    )


def write_watermark(conn: sqlite3.Connection, watermark: Watermark) -> None:
    """Upsert one ``sync_state`` row. Machine-local; never synced."""
    conn.execute(
        """
        INSERT INTO sync_state(
            peer, last_push_seq, last_pull_seq, last_sync_at, peer_generation
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(peer) DO UPDATE SET
            last_push_seq   = excluded.last_push_seq,
            last_pull_seq   = excluded.last_pull_seq,
            last_sync_at    = excluded.last_sync_at,
            peer_generation = excluded.peer_generation
        """,
        (
            watermark.peer,
            watermark.last_push_seq,
            watermark.last_pull_seq,
            watermark.last_sync_at,
            watermark.peer_generation,
        ),
    )


# ----- sequences -------------------------------------------------------------


def current_seq(conn: sqlite3.Connection) -> int:
    """Greatest ``hub_changelog.seq``; 0 on a machine that has never been a hub."""
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) FROM {HUB_CHANGELOG_TABLE}"
    ).fetchone()
    return 0 if row is None else int(row[0])


def local_seq(conn: sqlite3.Connection) -> int:
    """Greatest ``local_changelog.seq``: this machine's own write counter.

    Read BEFORE selecting the rows to offer, and stored as the new
    ``last_push_seq`` only after the push lands. A local write that commits
    during the round trip therefore lands ABOVE the recorded fence and is
    offered on the next sync, instead of falling into the gap that lost rows
    in round 1.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) FROM {LOCAL_CHANGELOG_TABLE}"
    ).fetchone()
    return 0 if row is None else int(row[0])


def settled_push_seq(
    *, previous: int, ceiling: int, held_seq: int | None, undelivered: bool
) -> int:
    """The push fence to record after one round trip. Round 5 gate B1.

    ``last_push_seq`` means "every ``local_changelog`` entry at or below this
    was offered AND the peer decided it". Stamping ``ceiling`` on it
    regardless of what happened is what lost rows: a row this machine held
    back, or one the peer refused, then sat below the new fence, and the
    fenced selection ``seq > last_push_seq`` can never name it again. The
    row's only remaining copy is local, the digest stops agreeing once the
    quarantine that excused it clears, and no repair on either machine
    re-offers it.

    One rule -- never step over a row that did not arrive:

    * ``undelivered``: the peer decided FEWER rows than it was given
      (``accepted + rejected`` below the rows offered). WHICH ones it refused
      is not reported, so the fence stays exactly where it was and the whole
      window is offered again. Measured from the peer's own conservation law
      rather than from its quarantine counter: a peer that drops rows for a
      reason nobody has thought of yet is caught by the same check, and a
      peer too old to report a counter cannot read as "reported zero".
    * ``held_seq``: the lowest changelog seq THIS machine held back. The
      fence stops one below it, so that entry and everything after it is
      selected again on every sync until the row can travel. A row a FULL
      offer held back has no seq at all; those are re-logged instead
      (:func:`apps.sync_hub.engine_changes.relog_held`).
    * otherwise ``ceiling``: everything selected arrived. The ordinary case,
      and the one that keeps the fence moving -- a fence that never advances
      re-offers the whole library forever, which is the cost this fence
      exists to remove.

    ``previous`` is a floor, so the fence never goes backwards and the
    re-offering is bounded by the writes made since the quarantine began.
    """
    if undelivered:
        return previous
    if held_seq is None:
        return ceiling
    return max(previous, min(ceiling, held_seq - 1))


__all__ = [
    "Watermark",
    "current_seq",
    "local_seq",
    "read_watermark",
    "settled_push_seq",
    "write_watermark",
]
