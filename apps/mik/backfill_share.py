"""``backfill share``: give a complete donor's MIK energy to its empty duplicates.

A row that already holds a MIK energy AND a MIK energy series (a complete
donor) is one recording with every other row of its duplicate group
(:mod:`apps.mik.duplicates`). A present member of that group with no energy
at all and no MIK series is filled from the donor: the scalar and the series,
written through the loader's normal path (source ``mik``, the loader's
confidence, history rows like any write, the equivalence gate and precedence
checks). Status counts those members as ``would_gain_by_duplicate_sharing``;
no analysis reaches them, only this operation does.

Never written:

* a member with an energy of any source, or with its own MIK series;
* a group whose complete donors disagree (energy or series): reported
  ``conflicting``;
* anything, while the equivalence gate reads other than ``passed``.

A dry run unless ``live``. A second live run writes nothing.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.shared.equivalence import EquivalenceGate
from apps.shared.state import db as state_db

from . import duplicates as dups
from . import load as loader
from .backfill import Library
from .backfill_picture import energy_picture
from .match import Match, MatchReport
from .mikdb import MikSong
from .share_plan import Donor, GroupShare, complete_donors, plan_groups

ACTOR = "apps.mik.backfill.share"
FIELDS: frozenset[str] = frozenset({"energy", loader.SEGMENTS_FIELD})


# ------------------------------------------------------------------ writing


def _donor_song(row_id: int, donor: Donor) -> MikSong:
    return MikSong(
        row_id=row_id,
        title=None,
        artist=None,
        album=None,
        path=None,
        path_error=None,
        key_camelot=None,
        key_confidence=None,
        energy=donor.energy,
        bpm=None,
        loudness=None,
        clipped_peak_count=None,
        analysed_at=donor.analysed_at,
        segments=donor.series,
    )


def _load_plan(conn: sqlite3.Connection, gate: EquivalenceGate, shares: Sequence[GroupShare]) -> loader.LoadPlan:
    """The loader's own plan for the fills: each group's first donor feeds its members."""
    songs: list[MikSong] = []
    report = MatchReport()
    shared: dict[int, tuple[Match, ...]] = {}
    donor_ids: set[str] = set()
    for offset, share in enumerate((share for share in shares if not share.conflicting), start=1):
        donor = share.donors[0]
        songs.append(_donor_song(-offset, donor))
        report.matches[-offset] = Match(-offset, donor.stable_id, "exact_path")
        shared[-offset] = tuple(Match(-offset, target, dups.SHARE_TIER) for target in share.fills)
        donor_ids.add(donor.stable_id)
    plan = loader.build_plan(songs, report, gate, conn, fields=FIELDS, shared=shared)
    # The donor's own values are already in place: only the members are written.
    plan.field_writes = [write for write in plan.field_writes if write.stable_id not in donor_ids]
    plan.segment_writes = [write for write in plan.segment_writes if write.stable_id not in donor_ids]
    return plan


def _applied(result: loader.ApplyResult | None) -> dict[str, int] | None:
    if result is None:
        return None
    return {
        "fields_written": result.fields_written,
        "fields_unchanged": result.fields_unchanged,
        "segment_tracks": result.segment_tracks,
        "segment_rows": result.segment_rows,
    }


def _without_pending_conflicts(
    conn: sqlite3.Connection, shares: list[GroupShare], by_metadata: bool, store: Path | None
) -> tuple[list[GroupShare], list[str]]:
    """Drop fills that pending evidence decides instead: such a row is not shared to.

    Pending evidence is a staged analysis, or a song of an unloaded ``store``.
    A row that evidence binds directly gets its OWN analysis when the store
    is loaded, and a row it disagrees about gets neither. Status decides both
    (:func:`energy_picture`); share reads the same picture, so it can never
    write a donor's value over a pending analysis of that row.
    """
    picture = energy_picture(conn, store=store, by_metadata=by_metadata)
    pending = set(picture.conflicting) | picture.gain_unique | picture.pending_scalar_only
    kept: list[GroupShare] = []
    held: list[str] = []
    for group in shares:
        fills = tuple(stable_id for stable_id in group.fills if stable_id not in pending)
        held.extend(stable_id for stable_id in group.fills if stable_id in pending)
        kept.append(dataclasses.replace(group, fills=fills))
    return kept, sorted(held)


def _share_decision(
    conn: sqlite3.Connection, share_across_duplicates: bool, store: Path | None
) -> tuple[list[GroupShare], list[str]]:
    """Every input to a share, from groups to pending holds, read from ``conn``."""
    groups = dups.duplicate_groups(conn, by_metadata=share_across_duplicates)
    return _without_pending_conflicts(conn, plan_groups(conn, groups), share_across_duplicates, store)


def share(
    library: Library, *, live: bool = False, share_across_duplicates: bool = False, store: Path | None = None
) -> dict[str, Any]:
    """Fill every empty present duplicate of a complete donor. Dry run unless ``live``.

    ``store`` is a COPY of a Mixed In Key store not yet loaded: rows it would
    settle itself are held, never filled from a donor.
    """
    gate = EquivalenceGate.load(library.data_dir)
    conn = state_db.open_rw(library.state_db) if live else state_db.open_ro(library.state_db)
    try:
        shares, held = _share_decision(conn, share_across_duplicates, store)
        plan = _load_plan(conn, gate, shares)
        # A relink, availability refresh, identity update or newly staged
        # analysis committed after this point can change who is a duplicate
        # or what is pending while target fields stay empty, so the whole
        # decision is re-derived under the lock and must come out the same.
        plan.still_valid = lambda locked: _share_decision(locked, share_across_duplicates, store) == (shares, held)
        applied = loader.apply_plan(conn, plan, actor=ACTOR) if live else None
    finally:
        conn.close()
    sharing = [share for share in shares if not share.conflicting]
    return {
        "mode": "live" if live else "dry-run",
        "state_db": str(library.state_db),
        "share_across_duplicates": share_across_duplicates,
        "store": str(store) if store else None,
        "equivalence": {name: plan.gate_summary[name] for name in sorted(FIELDS)},
        "counts": {
            "donors": sum(len(share.donors) for share in shares),
            "groups": len(shares),
            "rows_to_fill": sum(len(share.fills) for share in sharing),
            "conflicting_groups": len(shares) - len(sharing),
            "held_for_pending_analysis": len(held),
        },
        "planned": {"field_writes": len(plan.field_writes), "segment_tracks": len(plan.segment_writes)},
        "blocked_by_gate": plan.blocked_by_gate,
        "blocked_by_precedence": plan.blocked_by_precedence,
        "groups": [share.payload() for share in shares],
        "applied": _applied(applied),
        "stale_plan": applied.stale_plan if applied is not None else None,
    }


__all__ = ["ACTOR", "Donor", "GroupShare", "complete_donors", "plan_groups", "share"]
