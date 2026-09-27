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
  evidence (loser AND survivor both live there); only a remap the hub
  contradicts is dropped, and only when
  :func:`stale_remaps_behind_all_divergence` proves the remaps explain
  every divergent row. A mismatch with any second cause keeps them and
  raises as before, because the seq-0 re-pull would overwrite that cause.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from apps.shared.state import normalize_locations
from apps.sync_hub import capabilities, digest_diff, sync_set
from apps.sync_hub.client_transport_ops import _rows_from, _transaction
from apps.sync_hub.engine_identity_map import (
    _remove_remap_loser,
    load_identity_remap,
    record_identity_remap,
)
from apps.sync_hub.engine_watermark import Watermark, write_watermark
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
    known_stale: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """Stale remaps whose survivor THIS offer names, checked before it is pushed.

    A persisted remap moves the loser's children onto the survivor, and a
    moved row keeps any pending changelog entry, so a push offers it keyed to
    the survivor. When the hub contradicts that remap those rows belong to
    the loser: pushing them re-keys the loser's data on the hub, and the
    seq-0 replay would then adopt it and converge over the damage. ``rows``
    is the exact offer about to be pushed, so nothing logged later escapes
    the check. ``known_stale`` are pairs already proven stale and dropped by
    this sync's recovery: no longer persisted, still not safe to spread.
    The hub is asked only when the offer names a persisted survivor outside
    ``tracks``, so an ordinary round sends no request.
    """
    persisted = set(load_identity_remap(conn).values())
    at_risk = _survivors_named_by(rows, persisted | {survivor for _, survivor in known_stale})
    if not at_risk:
        return []
    spread = {pair for pair in known_stale if pair[1] in at_risk}
    if at_risk & persisted:
        spread.update(
            pair for pair in stale_identity_remaps(channel, conn, machine_id) if pair[1] in at_risk
        )
    return sorted(spread)


def _survivors_named_by(rows: list[RowChange], survivors: set[str]) -> set[str]:
    """Survivors a child row or a playlist's membership bundle points at."""
    named: set[str] = set()
    for row in rows:
        if row.table == "tracks":
            continue
        named.add(str(row.values.get("stable_id")))
        named.update(str(member.get("stable_id")) for member in row.members or ())
    return named & survivors


def stale_remaps_behind_all_divergence(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    divergent: Sequence[str],
) -> list[tuple[str, str]]:
    """Stale remaps, returned ONLY when they explain every divergent row.

    Dropping a remap re-pulls the hub from seq 0, and that replay rewrites
    any local row the hub ties or beats. Run on a mismatch with a second
    cause it would overwrite that cause's evidence (an unstamped local edit
    loses the exact tie) and report success over it. So ``divergent`` is
    diffed row by row, in full, against the hub, and every differing row
    must be one of the stale losers' own hub rows
    (:func:`_hub_rows_owned_by`) or a local row that IS one of them with
    only ``stable_id`` rewritten to the survivor
    (:func:`_moved_from_a_loser`). Anything else keeps every remap: the
    caller's original outcome stands. No ``divergent`` table is nothing to
    attribute, so it keeps them too.
    """
    persisted = load_identity_remap(conn)
    if not divergent or not persisted:
        return []
    hub_rows: HubRows = {"tracks": digest_diff.fetch_hub_rows(channel, machine_id, "tracks")}
    stale = _both_halves_live(persisted, hub_rows["tracks"])
    if not stale:
        return []
    for table in divergent:
        if table not in hub_rows:
            hub_rows[table] = digest_diff.fetch_hub_rows(channel, machine_id, table)
    claims = _claimed_loser_rows(conn, divergent, stale, hub_rows)
    owned = _hub_rows_owned_by(channel, machine_id, {loser for loser, _ in stale})
    unexplained = [row for row, claim in claims if claim not in owned]
    if unexplained:
        log.error(
            "kept %d stale identity remap(s) %s: %d divergent row(s) in %s are "
            "not the remaps' doing, and the seq-0 re-pull that drops them would "
            "overwrite that other cause. Examples: %s",
            len(stale),
            stale,
            len(unexplained),
            sorted({table for table, _pk in unexplained}),
            [digest_diff.format_pk(table, pk) for table, pk in unexplained[:5]],
        )
        return []
    return stale


def _claimed_loser_rows(
    conn: sqlite3.Connection,
    divergent: Sequence[str],
    stale: list[tuple[str, str]],
    hub_rows: HubRows,
) -> list[tuple[RowKey, RowKey | None]]:
    """Every divergent row, paired with the loser hub row it would have to be.

    A hub-only row claims itself. A local row claims the hub row it
    reproduces once ``stable_id`` is put back (``None`` if none): at its own
    pk when the hub holds that pk, since a remap rewrites such a row in
    place, or at any pk when it is local-only, since a copy lands under a
    new one. Only claims, because whether the claimed row really is a
    loser's is read from the hub AFTER this walk.
    """
    losers_of: dict[str, list[str]] = {}
    for loser, survivor in stale:
        losers_of.setdefault(survivor, []).append(loser)
    claims: list[tuple[RowKey, RowKey | None]] = []
    for table in divergent:
        by_pk = hub_rows[table]
        key_columns = sync_set.spec_for(table).pk
        local_pks: set[tuple[str, ...]] = set()
        for pk, local_hex, canonical in digest_diff.iter_eligible_local_rows(conn, table):
            local_pks.add(pk)
            hub_row = by_pk.get(pk)
            if hub_row is not None and hub_row.canonical_hex == local_hex:
                continue
            origin = _moved_from_a_loser(canonical, key_columns, losers_of, by_pk)
            in_place_or_new = hub_row is None or origin == pk
            claim = (table, origin) if origin is not None and in_place_or_new else None
            claims.append(((table, pk), claim))
        claims.extend(((table, pk), (table, pk)) for pk in by_pk if pk not in local_pks)
    return claims


def _hub_rows_owned_by(
    channel: HubTransport, machine_id: str, losers: Collection[str]
) -> set[RowKey]:
    """Every hub row keyed to a loser: its ``tracks`` row, children, memberships.

    Read from the hub's identity-repair bundle (``GET /pull`` with
    ``bundle_stable_ids``), which returns rows without moving any cursor. A
    playlist row in it only carries the membership list; the playlist
    itself is not the loser's.
    """
    payload = channel.get(
        f"{API_PREFIX}/pull",
        {
            "machine_id": machine_id,
            "bundle_stable_ids": sorted(losers),
            "capabilities": capabilities.QUARANTINE_V1,
        },
    )
    membership_pk = pk_columns(MEMBERSHIP_TABLE)
    owned: set[RowKey] = set()
    for row in _rows_from(payload, "pull"):
        if row.table != "playlists":
            owned.add((row.table, row.pk))
            continue
        owned.update(
            (MEMBERSHIP_TABLE, tuple(str(member[column]) for column in membership_pk))
            for member in row.members or ()
            if member.get("stable_id") in losers
        )
    return owned


def _moved_from_a_loser(
    canonical: Mapping[str, Any],
    key_columns: Sequence[str],
    losers_of: Mapping[str, list[str]],
    by_pk: Mapping[tuple[str, ...], digest_diff.HubRowSample],
) -> tuple[str, ...] | None:
    """The hub pk this local row reproduces byte for byte once ``stable_id``
    is put back to one of its survivor's losers, else ``None``. A table with
    no ``stable_id`` column holds no moved rows."""
    survivor = canonical.get("stable_id")
    if not isinstance(survivor, str):
        return None
    for loser in losers_of.get(survivor, ()):
        origin = {**canonical, "stable_id": loser}
        origin_pk = tuple(str(origin[column]) for column in key_columns)
        hub_row = by_pk.get(origin_pk)
        if hub_row is not None and hub_row.canonical_hex == digest_diff.canonical_hex(origin):
            return origin_pk
    return None


def drop_identity_remaps(
    conn: sqlite3.Connection, stale: list[tuple[str, str]], replay: Watermark
) -> None:
    """Delete the stale remap rows AND record the seq-0 replay, in one transaction.

    ``replay`` is the watermark the re-pull starts from. Committing it with
    the deletes is what makes the recovery resumable: once the remaps are
    gone this spoke can no longer see that it needs one, so a replay cut off
    by a transport error must still be owed. The next sync then starts its
    ordinary pull from seq 0 and lands the losers itself.
    """
    remap = load_identity_remap(conn)
    with _transaction(conn):
        for loser, _survivor in stale:
            _remove_remap_loser(conn, remap, loser)
        write_watermark(conn, replay)
    log.warning(
        "dropped %d persisted identity remap(s) the hub contradicts: it serves "
        "each loser and its survivor as two independent live tracks (ADR-0074 "
        "hub identity authority). Re-pulling from seq 0 so the losers and their "
        "children land under their own ids: %s",
        len(stale),
        stale,
    )


def restore_identity_remaps(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> None:
    """Re-persist remaps this sync dropped, because their moved rows cannot ship.

    Without them the next sync's guard no longer knows the survivor is
    carrying the loser's rows and would push them unchecked. The seq-0 pull
    watermark written with the drop stays: a full re-pull is harmless.
    """
    remap = load_identity_remap(conn)
    with _transaction(conn):
        for loser, survivor in pairs:
            record_identity_remap(conn, remap, loser, survivor)


__all__ = [
    "collapse_location_twins",
    "drop_identity_remaps",
    "restore_identity_remaps",
    "stale_identity_remaps",
    "stale_remaps_behind_all_divergence",
    "stale_remaps_the_offer_would_spread",
]
