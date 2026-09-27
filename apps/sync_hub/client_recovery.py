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
  contradicts is dropped.
"""
from __future__ import annotations

import logging
import sqlite3

from apps.shared.state import normalize_locations
from apps.sync_hub import digest_diff, engine
from apps.sync_hub.client_transport_ops import _transaction
from apps.sync_hub.engine_identity_map import _remove_remap_loser, load_identity_remap
from apps.sync_hub.engine_watermark import Watermark, write_watermark
from apps.sync_hub.protocol import RowChange
from apps.sync_hub.transport import HubTransport

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
    hub_tracks = {pk[0] for pk in digest_diff.fetch_hub_rows(channel, machine_id, "tracks")}
    return sorted(
        (loser, survivor)
        for loser, survivor in persisted.items()
        if loser in hub_tracks and survivor in hub_tracks
    )


def stale_remaps_the_offer_would_spread(
    channel: HubTransport, conn: sqlite3.Connection, machine_id: str, watermark: Watermark
) -> list[tuple[str, str]]:
    """Stale remaps whose survivor the NEXT push names, checked before it leaves.

    A persisted remap moves the loser's children onto the survivor, and a
    moved row keeps any pending changelog entry, so the next push offers it
    keyed to the survivor. When the hub contradicts that remap those rows
    belong to the loser: pushing them re-keys the loser's data on the hub,
    and the seq-0 replay would then adopt it and converge over the damage.
    The offer is the one :func:`apps.sync_hub.engine.spoke_push` selects, and
    the hub is asked only when it names a persisted survivor outside
    ``tracks``, so an ordinary sync pays one local selection and no request.
    """
    remap = load_identity_remap(conn)
    if not remap:
        return []
    offer = engine.spoke_push(conn, watermark=watermark, ceiling=engine.local_seq(conn))
    at_risk = _survivors_named_by(offer.rows, set(remap.values()))
    if not at_risk:
        return []
    return [
        (loser, survivor)
        for loser, survivor in stale_identity_remaps(channel, conn, machine_id)
        if survivor in at_risk
    ]


def _survivors_named_by(rows: list[RowChange], survivors: set[str]) -> set[str]:
    """Survivors a child row or a playlist's membership bundle points at."""
    named: set[str] = set()
    for row in rows:
        if row.table == "tracks":
            continue
        named.add(str(row.values.get("stable_id")))
        named.update(str(member.get("stable_id")) for member in row.members or ())
    return named & survivors


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


__all__ = [
    "collapse_location_twins",
    "drop_identity_remaps",
    "stale_identity_remaps",
    "stale_remaps_the_offer_would_spread",
]
