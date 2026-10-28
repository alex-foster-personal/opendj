"""Which present rows a complete MIK donor can fill: ONE answer for status and share.

:func:`plan_groups` is the only place this is decided. ``backfill status``
counts its fills as ``would_gain_by_duplicate_sharing`` and ``backfill share``
writes exactly them, so the number status prints is the number share fills.

A donor is a row holding a MIK energy (a whole number) AND a MIK series. A
fill is a PRESENT member of the donor's duplicate group with no energy field
of any source and no MIK series. A group whose donors disagree (energy or
series) is conflicting: its fills are never written.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import SOURCE
from . import duplicates as dups
from .mikdb import EnergySegment

DENOMINATOR = "present"
"""``track_availability.state`` of a row whose audio is on disk: every count's denominator."""


@dataclass(frozen=True)
class Donor:
    """A row with a MIK energy AND a MIK series: what it can give."""

    stable_id: str
    energy: int
    analysed_at: str
    series: tuple[EnergySegment, ...]

    def answer(self) -> tuple[int, tuple[EnergySegment, ...]]:
        return self.energy, self.series


@dataclass(frozen=True)
class GroupShare:
    """One duplicate group: its complete donors and the members they fill."""

    donors: tuple[Donor, ...]
    fills: tuple[str, ...]

    @property
    def conflicting(self) -> bool:
        return len({donor.answer() for donor in self.donors}) > 1

    def payload(self) -> dict[str, Any]:
        return {
            "donors": [donor.stable_id for donor in self.donors],
            "energy": None if self.conflicting else self.donors[0].energy,
            "fills": list(self.fills),
            "conflicting": self.conflicting,
        }


# ----------------------------------------------------------------- reading


def _mik_series(conn: sqlite3.Connection) -> dict[str, tuple[tuple[EnergySegment, ...], frozenset[str]]]:
    """``{stable_id: (segments, analysis times stamped on them)}`` for every MIK series."""
    query = (
        "SELECT stable_id, seq, start_ms, length_ms, energy, start_clamped, modified_at "
        "FROM track_energy_segments WHERE source = ? ORDER BY stable_id, seq"
    )
    series: dict[str, list[EnergySegment]] = {}
    stamps: dict[str, set[str]] = {}
    for stable_id, seq, start, length, energy, clamped, modified_at in conn.execute(query, (SOURCE,)):
        series.setdefault(stable_id, []).append(EnergySegment(seq, start, length, energy, bool(clamped)))
        stamps.setdefault(stable_id, set()).add(modified_at)
    return {stable_id: (tuple(segments), frozenset(stamps[stable_id])) for stable_id, segments in series.items()}


def _energy_rows(conn: sqlite3.Connection) -> dict[str, tuple[str, Any, str]]:
    """``{stable_id: (source, value, modified_at)}`` for every live energy field."""
    query = (
        "SELECT stable_id, source, value_json, modified_at FROM track_fields "
        "WHERE field_name = 'energy' AND deleted_at IS NULL"
    )
    return {row[0]: (row[1], json.loads(row[2]), row[3]) for row in conn.execute(query)}


def same_analysis_rows(conn: sqlite3.Connection) -> set[str]:
    """Rows whose MIK energy and whole MIK series come from ONE analysis.

    Both halves are stamped with the analysis time (``modified_at``). A series
    left over from an older analysis does not complete a newer scalar: the row
    would drop out of later plans, and sharing would pair the new value with
    the old curve. This is the single definition of a complete MIK row.
    """
    series = _mik_series(conn)
    return {
        stable_id
        for stable_id, (source, _value, modified_at) in _energy_rows(conn).items()
        if source == SOURCE and stable_id in series and series[stable_id][1] == {modified_at}
    }


def complete_donors(conn: sqlite3.Connection) -> dict[str, Donor]:
    """Every row holding a MIK energy (a whole number) AND the MIK series of the same analysis."""
    series = _mik_series(conn)
    complete = same_analysis_rows(conn)
    donors: dict[str, Donor] = {}
    for stable_id, (_source, value, modified_at) in _energy_rows(conn).items():
        whole = isinstance(value, int) and not isinstance(value, bool)
        if whole and stable_id in complete:
            donors[stable_id] = Donor(stable_id, value, modified_at, series[stable_id][0])
    return donors


def _present(conn: sqlite3.Connection) -> set[str]:
    query = (
        "SELECT tracks.stable_id FROM tracks JOIN track_availability "
        "ON track_availability.stable_id = tracks.stable_id "
        "WHERE tracks.deleted_at IS NULL AND track_availability.state = ?"
    )
    return {row[0] for row in conn.execute(query, (DENOMINATOR,))}


def plan_groups(conn: sqlite3.Connection, groups: dups.DuplicateGroups) -> list[GroupShare]:
    """Every group with a complete donor and the present members it would fill."""
    donors = complete_donors(conn)
    filled = set(_energy_rows(conn)) | set(_mik_series(conn))
    present = _present(conn)
    by_group: dict[str, list[Donor]] = {}
    for donor in donors.values():
        by_group.setdefault(groups.group_of(donor.stable_id), []).append(donor)
    shares: list[GroupShare] = []
    for members in by_group.values():
        group_donors = tuple(sorted(members, key=lambda donor: donor.stable_id))
        fills = tuple(
            sorted(
                stable_id
                for stable_id in groups.members_of(group_donors[0].stable_id)
                if stable_id in present and stable_id not in filled
            )
        )
        if fills:
            shares.append(GroupShare(group_donors, fills))
    return sorted(shares, key=lambda share: share.donors[0].stable_id)


__all__ = ["DENOMINATOR", "Donor", "GroupShare", "complete_donors", "plan_groups"]
