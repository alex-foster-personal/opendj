"""Spoke-side recovery from the two proven causes of a permanent digest brick.

Neither is a repair pass in the ADR 04 c6 sense. Each acts only on one
defect class whose cause is established BEFORE anything changes, and every
other divergence still raises ``SyncDigestMismatch`` exactly as before
(docs/decisions ADR slug ``cloudsync-identified-brick-recovery``). Both
reproduced on the Air, Sun 27 Sep 2026:

* **NFC location twins** (round 2 finding N2, round 3 finding R3). A legacy
  NFD ``track_locations`` row and its NFC twin are one file under two
  spellings. The hub collapsed them on arrival; a spoke that wrote the pair
  kept both, so its ``track_locations`` digest never matched again.
  :func:`collapse_location_twins` runs
  :mod:`apps.shared.state.normalize_locations`, the repair path that module
  was written to be, before EVERY offer. It makes the same LWW decision the
  hub already made, so it cannot disagree with the fleet.
* **Stale persisted identity remaps.** A ``sync_identity_remap`` row says
  loser -> survivor, so this spoke holds the loser out of its offer and
  digest and moves the loser's children onto the survivor. When the HUB
  counts that loser as an independent, sync-eligible track, the remap
  contradicts hub identity authority (ADR-0074) and the spoke can never
  converge. :func:`stale_identity_remaps` asks the hub for exactly that
  evidence (loser AND survivor both live there). Only a remap the hub
  contradicts is retired, only when the remaps explain every divergent row,
  and only in one write-locked transaction that proves it and repairs it
  together (:func:`retire_stale_remaps`). A mismatch with any second cause
  keeps them and raises as before.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from apps.shared.state import normalize_locations
from apps.sync_hub import capabilities, digest_diff, engine
from apps.sync_hub.client_transport_ops import _machines_from, _rows_from, _transaction
from apps.sync_hub.engine_identity_map import _remove_remap_loser, load_identity_remap
from apps.sync_hub.protocol import MEMBERSHIP_TABLE, RowChange, pk_columns
from apps.sync_hub.transport import API_PREFIX, HubTransport

#: ``table -> {pk: GET /rows sample}``, fetched once per recovery decision.
HubRows = dict[str, dict[tuple[str, ...], digest_diff.HubRowSample]]
#: ``(table, pk)`` of one row.
RowKey = tuple[str, tuple[str, ...]]

log = logging.getLogger(__name__)


# ----- NFC location twins -----------------------------------------------------


def collapse_location_twins(conn: sqlite3.Connection) -> int:
    """Collapse NFD/NFC ``track_locations`` twins before the offer.

    Returns the number of twin GROUPS collapsed, each of which deleted at
    least one loser (0 on a clean library). A lone NFD row rewritten NFC in
    place is repaired too but is not a twin, so it is logged, not counted. A
    group holding an unorderable stamp is left alone and reported:
    hard-deleting on a comparison that was never made is the loss
    ``normalize_locations`` refuses, and the brick it leaves is loud, bounded
    and fixed by one command. The unlocked :func:`normalize_locations.scan`
    only decides whether to take the write lock; the winners are elected
    again under it by :func:`normalize_locations.collapse_all`.
    """
    quarantined = normalize_locations.quarantined_groups(conn)
    if quarantined:
        log.error(
            "%d track_locations NFC twin group(s) hold an unorderable stamp and "
            "were NOT collapsed; run `python -m apps.shared.state.normalize_stamps "
            "--live` on this machine, then sync again. Example location ids: %s",
            len(quarantined),
            [member.location_id for member in quarantined[0]],
        )
    if not normalize_locations.scan(conn):
        return 0
    collapses = normalize_locations.collapse_all(conn)
    twins = [collapse for collapse in collapses if collapse.losers]
    log.warning(
        "collapsed %d track_locations NFC twin group(s) before the offer, "
        "dropping %d loser row(s) that lost last-writer-wins to their twin, "
        "and rewrote %d lone non-NFC location(s) in place: %s",
        len(twins),
        sum(len(collapse.losers) for collapse in twins),
        len(collapses) - len(twins),
        [collapse.describe() for collapse in collapses[:5]],
    )
    return len(twins)


# ----- stale persisted identity remaps -----------------------------------------


def stale_identity_remaps(
    channel: HubTransport, conn: sqlite3.Connection, machine_id: str
) -> list[tuple[str, str]]:
    """Persisted ``(loser, survivor)`` pairs the hub serves as TWO live tracks.

    Evidence, not inference: the hub's ``GET /rows`` lists only rows in its
    own sync set, so a pair appearing there in full is two tracks the hub
    counts as independent -- neither collapsed nor held by its own identity
    election. Both halves are required: a live loser beside an ABSENT
    survivor is the hub holding the opposite verdict, which the reversed
    remap repair in ``apply_hub_identity_rejects`` already settles. A spoke
    with no persisted remap asks nothing. Sorted for a stable log.
    """
    persisted = load_identity_remap(conn)
    if not persisted:
        return []
    return _both_halves_live(persisted, digest_diff.fetch_hub_rows(channel, machine_id, "tracks"))


def _both_halves_live(
    persisted: Mapping[str, str], hub_tracks: Mapping[tuple[str, ...], Any]
) -> list[tuple[str, str]]:
    live = {pk[0] for pk in hub_tracks}
    return sorted(
        (loser, survivor)
        for loser, survivor in persisted.items()
        if loser in live and survivor in live
    )


def stale_remaps_the_offer_would_spread(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    rows: list[RowChange],
) -> list[tuple[str, str]]:
    """Stale remaps whose survivor THIS offer names, checked before it is pushed.

    A persisted remap moves the loser's children onto the survivor, and a
    moved row keeps any pending changelog entry, so a push offers it keyed to
    the survivor. When the hub contradicts that remap those rows belong to
    the loser: pushing them re-keys the loser's data on the hub, where no
    recovery can tell them from the survivor's own. ``rows`` is the exact
    offer about to be pushed, so nothing logged later escapes the check. The
    hub is asked only when the offer names a persisted survivor outside
    ``tracks``, so an ordinary round sends no request.
    """
    at_risk = _survivors_named_by(rows, set(load_identity_remap(conn).values()))
    if not at_risk:
        return []
    return [pair for pair in stale_identity_remaps(channel, conn, machine_id) if pair[1] in at_risk]


def _survivors_named_by(rows: list[RowChange], survivors: set[str]) -> set[str]:
    """Survivors a child row or a playlist's membership bundle points at."""
    named: set[str] = set()
    for row in rows:
        if row.table == "tracks":
            continue
        named.add(str(row.values.get("stable_id")))
        named.update(str(member.get("stable_id")) for member in row.members or ())
    return named & survivors


def retire_stale_remaps(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    hub_machine_id: str,
    divergent: Sequence[str],
) -> list[tuple[str, str]]:
    """Retire the stale remaps, ONLY if they explain every divergent row.

    Every hub read happens first: the sync-eligible rows of ``tracks`` and
    of each ``divergent`` table (``GET /rows``), and the losers' identity-
    repair bundle (``GET /pull`` with ``bundle_stable_ids``, which moves no
    cursor). Then ONE ``BEGIN IMMEDIATE`` transaction attributes and
    repairs together, so no local write can land between the proof and the
    change: every differing local row must be one of the losers' own hub
    rows (:func:`_hub_rows_owned_by`), or a local row that IS one of them
    with only ``stable_id`` rewritten in place (:func:`_moved_from_a_loser`).
    If so, the remaps are deleted and the bundles applied with the pull's
    own LWW, which restores each in-place row on its exact-stamp tie and
    lands the rest; nothing newer is ever overwritten. A crash rolls it all
    back, so no moment exists with the remap gone and a moved row left.

    Anything unexplained changes nothing and returns ``[]``: the caller's
    original outcome stands, because an ordinary pull would overwrite that
    other cause on a tie. That includes a copy the old remap made under a
    new key, which the bundle cannot take back. No ``divergent`` table is
    nothing to attribute, so it returns ``[]`` too.
    """
    persisted = load_identity_remap(conn)
    if not divergent or not persisted:
        return []
    hub_rows: HubRows = {"tracks": digest_diff.fetch_hub_rows(channel, machine_id, "tracks")}
    stale = _both_halves_live(persisted, hub_rows["tracks"])
    if not stale:
        return []
    hub_rows.update(_hub_rows_of(channel, machine_id, divergent, skip=hub_rows.keys()))
    bundle = _loser_bundles(channel, machine_id, [loser for loser, _ in stale])
    unexplained = _retire_if_every_row_is_theirs(
        conn, hub_machine_id, divergent, stale, hub_rows, bundle
    )
    if unexplained:
        log.error(
            "kept %d stale identity remap(s) %s: %d divergent row(s) in %s are "
            "not the remaps' doing, and retiring them would overwrite that "
            "other cause. Examples: %s",
            len(stale),
            stale,
            len(unexplained),
            sorted({table for table, _pk in unexplained}),
            [digest_diff.format_pk(table, pk) for table, pk in unexplained[:5]],
        )
        return []
    log.warning(
        "retired %d persisted identity remap(s) the hub contradicts: it serves "
        "each loser and its survivor as two independent live tracks (ADR-0074 "
        "hub identity authority). Every divergent row was theirs; applied the "
        "losers' %d hub bundle row(s) so they and their children stand under "
        "their own ids: %s",
        len(stale),
        len(bundle["rows"]),
        stale,
    )
    return stale


def _hub_rows_of(
    channel: HubTransport, machine_id: str, tables: Sequence[str], *, skip: Collection[str]
) -> HubRows:
    """The hub's sync-eligible rows of each table not already in ``skip``."""
    return {
        table: digest_diff.fetch_hub_rows(channel, machine_id, table)
        for table in tables
        if table not in skip
    }


def _retire_if_every_row_is_theirs(
    conn: sqlite3.Connection,
    hub_machine_id: str,
    divergent: Sequence[str],
    stale: list[tuple[str, str]],
    hub_rows: HubRows,
    bundle: Mapping[str, Any],
) -> list[RowKey]:
    """Under ONE write lock: attribute every divergent row, then retire and repair.

    Returns the rows the stale remaps do not explain; when there are any,
    nothing was written. Otherwise the remaps are deleted and the bundle
    applied with the pull's own merge in the same transaction.
    """
    owned = _hub_rows_owned_by(bundle, {loser for loser, _ in stale})
    with _transaction(conn, immediate=True):
        claims = _claimed_loser_rows(conn, divergent, stale, hub_rows)
        unexplained = [row for row, claim in claims if claim not in owned]
        if unexplained:
            return unexplained
        remap = load_identity_remap(conn)
        for loser, _survivor in stale:
            _remove_remap_loser(conn, remap, loser)
        engine.merge_machines(conn, _machines_from(bundle, "pull"), caller_id=hub_machine_id)
        engine.spoke_apply(conn, _rows_from(bundle, "pull"))
    return []


def _loser_bundles(
    channel: HubTransport, machine_id: str, losers: Sequence[str]
) -> Mapping[str, Any]:
    """The hub's identity-repair bundle for ``losers``: rows plus machines."""
    return channel.get(
        f"{API_PREFIX}/pull",
        {
            "machine_id": machine_id,
            "bundle_stable_ids": sorted(losers),
            "capabilities": capabilities.QUARANTINE_V1,
        },
    )


def _hub_rows_owned_by(bundle: Mapping[str, Any], losers: Collection[str]) -> set[RowKey]:
    """Every bundle row keyed to a loser: its ``tracks`` row, children, memberships.

    A playlist row in the bundle only carries the membership list; the
    playlist itself is not the loser's.
    """
    membership_pk = pk_columns(MEMBERSHIP_TABLE)
    owned: set[RowKey] = set()
    for row in _rows_from(bundle, "pull"):
        if row.table != "playlists":
            owned.add((row.table, row.pk))
            continue
        owned.update(
            (MEMBERSHIP_TABLE, tuple(str(member[column]) for column in membership_pk))
            for member in row.members or ()
            if member.get("stable_id") in losers
        )
    return owned


def _claimed_loser_rows(
    conn: sqlite3.Connection,
    divergent: Sequence[str],
    stale: list[tuple[str, str]],
    hub_rows: HubRows,
) -> list[tuple[RowKey, RowKey | None]]:
    """Every divergent row, paired with the loser hub row it would have to be.

    A hub-only row claims itself. A local row claims its own pk when it
    reproduces the hub row there once ``stable_id`` is put back from the
    survivor to a loser (a remap rewrites such a row in place), else
    ``None``.
    """
    losers_of: dict[str, list[str]] = {}
    for loser, survivor in stale:
        losers_of.setdefault(survivor, []).append(loser)
    claims: list[tuple[RowKey, RowKey | None]] = []
    for table in divergent:
        by_pk = hub_rows[table]
        local_pks: set[tuple[str, ...]] = set()
        for pk, local_hex, canonical in digest_diff.iter_eligible_local_rows(conn, table):
            local_pks.add(pk)
            hub_row = by_pk.get(pk)
            if hub_row is not None and hub_row.canonical_hex == local_hex:
                continue
            moved = hub_row is not None and _moved_from_a_loser(canonical, hub_row, losers_of)
            claims.append(((table, pk), (table, pk) if moved else None))
        claims.extend(((table, pk), (table, pk)) for pk in by_pk if pk not in local_pks)
    return claims


def _moved_from_a_loser(
    canonical: Mapping[str, Any],
    hub_row: digest_diff.HubRowSample,
    losers_of: Mapping[str, list[str]],
) -> bool:
    """Whether this local row is the hub row at its pk, byte for byte, once
    ``stable_id`` is put back to one of its survivor's losers. A table with
    no ``stable_id`` column holds no moved rows."""
    survivor = canonical.get("stable_id")
    if not isinstance(survivor, str):
        return False
    return any(
        hub_row.canonical_hex == digest_diff.canonical_hex({**canonical, "stable_id": loser})
        for loser in losers_of.get(survivor, ())
    )


__all__ = [
    "collapse_location_twins",
    "retire_stale_remaps",
    "stale_identity_remaps",
    "stale_remaps_the_offer_would_spread",
]
