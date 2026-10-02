"""Stale-copy guard: a track the fleet dropped does not come back from a copy.

Contract: ``docs/decisions/ADR-NEW-deletes-stay-deleted.md`` ("Stale copies"),
CLOUDSYNC-30, issue #4628 criteria 1 and 2.

Requirements (status: done + ran + regression tests):

1. A live ``tracks`` row that another machine authored, and that the hub no
   longer holds, is refused.
   - [if] its ``origin_device_id`` names a machine registered on the hub,
     other than the caller [and] the hub stores no row under that
     ``stable_id`` [and] no identity remap names it [then] it is an orphan of
     the fleet and the push is refused.
   - [if] the caller authored the row itself [then] it is new local work
     and passes. This is the new-machine control.
   - [if] the row is a tombstone [then] it passes: a tombstone the hub lacks
     can only add a deletion.
2. A caller re-seeding a hub that went backwards passes.
   - [if] the caller declares ``reseed`` [then] nothing is refused, because a
     restored hub has forgotten rows it really held and the fleet's copies
     are how it gets them back.
3. A row this module cannot attribute is counted, never decided.
   - [if] the origin is empty or names no registered machine [then] the row
     is reported as unattributable and passes, as before this guard.

Why origin, not "first sync": in a hub-and-spoke fleet a spoke only ever
holds another machine's row because the hub gave it that row. A hub that does
not hold it any more dropped it, unless the hub itself was restored, which is
what ``reseed`` declares. The copy is the dangerous case precisely because it
carries those foreign rows, and a new machine has none to carry. Measured in
issue #4628: a stale copy held 166 tracks the fleet had dropped with no
tombstone, and its first sync would have pushed all of them back live.

The verdict is computed by the HUB, which alone knows what it holds, which
remaps it recorded and which machines are registered. The spoke asks before
it pushes (``POST /stale-check``, :mod:`apps.sync_hub.client_stale_copy`) so
that it can name every orphan at once, and ``POST /push`` applies the same
rule as a backstop.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from apps.sync_hub import protocol, protocol_common
from apps.sync_hub.engine_identity_map import load_identity_remap
from apps.sync_hub.protocol import RowChange

#: The 409 ``detail.code`` a refused push answers with.
CODE: str = "SYNC_STALE_TRACKS"

#: How many orphan ids a 409 or a log line names. The full set is what the
#: ``stale-tracks`` command lists.
EXAMPLES: int = 20


# -----------------------------------------------------------------------------
# types
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One live ``tracks`` row a caller is about to offer: id and author."""

    stable_id: str
    origin_device_id: str


@dataclass(frozen=True)
class Verdict:
    """What the hub decided about a set of candidates."""

    orphans: tuple[str, ...]
    unattributable: int = 0


class StaleTracksRefusedError(RuntimeError):
    """A push carried tracks the fleet dropped. Nothing in the batch applied."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        super().__init__(refusal_message(len(verdict.orphans), verdict.orphans[:EXAMPLES]))


# -----------------------------------------------------------------------------
# the rule
# -----------------------------------------------------------------------------


def candidates_from_changes(changes: Iterable[RowChange]) -> list[Candidate]:
    """Every LIVE ``tracks`` row in ``changes``. A tombstone is never a candidate."""
    return [
        Candidate(stable_id=str(change.pk[0]), origin_device_id=change.origin_device_id)
        for change in changes
        if change.table == protocol_common.TRACKS_TABLE
        and change.values.get(protocol.DELETED_AT) is None
    ]


def classify(
    conn: sqlite3.Connection,
    candidates: Sequence[Candidate],
    caller_id: str,
) -> Verdict:
    """Decide which of ``candidates`` are orphans of the fleet (rule 1, 3)."""
    foreign = [
        candidate
        for candidate in candidates
        if candidate.origin_device_id not in (protocol.NO_ORIGIN, caller_id)
    ]
    if not foreign:
        return Verdict(orphans=())
    stored = _present(
        conn, "SELECT stable_id FROM tracks", [candidate.stable_id for candidate in foreign]
    )
    registered = _present(
        conn,
        "SELECT machine_id FROM machines",
        sorted({candidate.origin_device_id for candidate in foreign}),
        column="machine_id",
    )
    remapped = load_identity_remap(conn)
    orphans: list[str] = []
    unattributable = 0
    for candidate in foreign:
        if candidate.stable_id in stored or candidate.stable_id in remapped:
            continue
        if candidate.origin_device_id not in registered:
            unattributable += 1
            continue
        orphans.append(candidate.stable_id)
    return Verdict(orphans=tuple(sorted(set(orphans))), unattributable=unattributable)


def refuse_orphans(
    conn: sqlite3.Connection,
    changes: Sequence[RowChange],
    caller_id: str,
    *,
    reseed: bool,
) -> Verdict:
    """The ``/push`` backstop: raise when ``changes`` would resurrect a track.

    Runs inside the push transaction before anything is applied, so a refusal
    rolls the whole batch back.
    """
    if reseed:
        return Verdict(orphans=())
    verdict = classify(conn, candidates_from_changes(changes), caller_id)
    if verdict.orphans:
        raise StaleTracksRefusedError(verdict)
    return verdict


def refusal_message(count: int, examples: Sequence[str]) -> str:
    """The reason and the remedy, in the words every surface shows."""
    return (
        f"refusing {count} track(s) another machine authored that the hub no "
        f"longer holds: the fleet dropped them, so this library is a stale or "
        f"copied database and pushing them would bring them back on every "
        f"machine. Example stable_id(s): {', '.join(examples)}. Nothing in "
        f"this push was applied. Remedy: run `python -m apps.sync_hub "
        f"stale-tracks --data-dir <dir> --hub <url>` to list them, then "
        f"`--remove` to mark them removed here (reversible with undelete), or "
        f"`--keep` to claim them as this machine's own and send them back to "
        f"the fleet."
    )


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _present(
    conn: sqlite3.Connection,
    select: str,
    keys: Sequence[str],
    *,
    column: str = "stable_id",
) -> frozenset[str]:
    """Which of ``keys`` the table behind ``select`` holds, in one read."""
    if not keys:
        return frozenset()
    return frozenset(
        str(row[0])
        for row in conn.execute(
            f"{select} WHERE {column} IN (SELECT value FROM json_each(?))",
            (json.dumps(list(keys)),),
        )
    )


__all__ = [
    "CODE",
    "EXAMPLES",
    "Candidate",
    "StaleTracksRefusedError",
    "Verdict",
    "candidates_from_changes",
    "classify",
    "refusal_message",
    "refuse_orphans",
]
