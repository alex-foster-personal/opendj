"""The spoke side: one full hello -> push -> pull -> digest round trip.

``run_sync`` is the whole spoke. It never repairs: if the two sides disagree
after a sync it raises :class:`SyncDigestMismatch` and stops, exactly as
ADR 04 c6 requires. A divergence means a bug in the merge, and a repair pass
would hide it behind data loss. The one carve-out is two defect classes whose
cause is PROVEN before anything changes (:mod:`apps.sync_hub.client_recovery`):
NFC location twins collapse before every offer, and a persisted identity
remap the hub contradicts is dropped and followed by one re-pull. Any other
mismatch still hard-stops.

Moving the bytes is :mod:`apps.sync_hub.transport`; the wire decoding,
batching and chunked HTTP calls are :mod:`apps.sync_hub.client_transport_ops`
(quality-gate file_size ratchet, round 4); the record a sync produces is
:mod:`apps.sync_hub.client_result` (same ratchet, round 5 final gate), and
the pure bookkeeping over a sync's rounds -- the per-round record, the
watermark the first round starts from, and the tally the last one produces
-- is :mod:`apps.sync_hub.client_rounds` (same ratchet, round 5 gate B1).
This module is the round trip itself -- when to run another round, when a
hub has gone backwards, when to give up and raise. Every submodule's names
are re-exported here because callers think of them as part of the client's
surface.

Three things ADR 08 changed here:

* **Both directions are chunked** (point 6, round 1 finding A2). The push is
  split into :data:`PUSH_BATCH_ROWS` requests and the pull loops on the
  hub's ``has_more`` -- an unchunked first sync holds the entire JSON
  body twice in memory on each side. Private corpus measurements are not
  included in the public source.
* **The push fence is a local sequence number** (point 3, finding 3), read
  BEFORE the rows are selected and written only after they land. Nothing in
  a watermark is a wall clock any more.
* **A digest mismatch is only believed once both sides have stopped moving**
  (round 2 findings 6b and N7, round 3 finding R6). The hub reports the
  changelog seq its digest describes; if that is above what this spoke
  pulled, another machine pushed during the round trip, and if this machine's
  own changelog advanced past the push fence, a local write landed mid-sync.
  Either way the sync runs ANOTHER round and compares again rather than
  raising, and keeps settling until the fence stops moving, bounded by
  :data:`MAX_ROUNDS`. Round 2 raised ``SyncDigestMismatch`` for this -- a
  corruption alarm that cleared itself on the next sync. Round 3 settled
  exactly once, so a fleet writing across two round trips still raised it.
  A fleet that never settles now raises :class:`SyncStillMoving`, a DISTINCT
  type, so "concurrent" and "corrupt" never share a message.
* **A hub that went backwards is detected and recovered from** (point 4,
  finding A3, re-keyed by round 2 finding N6). ``hello`` reports the hub's
  GENERATION TOKEN, not its max seq: a spoke that sees a different token
  than the one it stored concludes the hub was restored from a Litestream
  point in time, logs loudly, drops both floors to zero and re-offers
  everything. The replay is safe because LWW rejects on equality, which
  round 1 proved under three consecutive replays. Round 2 inferred the
  restore from the hub's ``MAX(seq)`` going backwards, which a routine
  changelog prune also does -- and paid a full library re-offer for it.
"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    capabilities,
    client_recovery,
    client_stale_copy,
    digest_diff,
    engine,
    protocol,
    spoke_credential,
    wire_version,
)
from apps.sync_hub.client_result import SyncResult
from apps.sync_hub.client_rounds import (
    _result_from_rounds,
    _Round,
    _watermark_after_hello,
)
from apps.sync_hub.client_stale_copy import SyncStaleTracks
from apps.sync_hub.client_transport_ops import (
    PUSH_BODY_MAX_BYTES,
    _fetch_hub_digest,
    _int_from,
    _local_machine_row,
    _machines_from,
    _pull_in_chunks,
    _push_in_batches,
    _PushOutcome,
    _run_identity_repair,
    _transaction,
    state_db_path,
)
from apps.sync_hub.engine_identity_map import (
    IdentityRepairRequest,
    apply_hub_identity_rejects,
    prepare_spoke_identity,
)
from apps.sync_hub.sync_timing import PhaseTimer, optional_span
from apps.sync_hub.transport import (
    API_PREFIX,
    DEFAULT_TIMEOUT_S,
    PUSH_TIMEOUT_S,
    HttpTransport,
    HubTransport,
    SyncTransportError,
)

log = logging.getLogger(__name__)

#: Rows per ``POST /push``. Each request opens a hub connection and commits
#: its own write transaction, and every commit rewrites the same index and
#: changelog pages, so 200 rows per request cost a 10,000-track first sync
#: 100 hub commits (LIBM-120 L6 round 6). At 1,000 the largest body of that
#: fixture's first offer is 0.63 MB, and one request's write transaction
#: stays well under the hub's busy timeout. Rows are not all that narrow, so
#: a batch also closes at :data:`PUSH_BODY_MAX_BYTES`, and a push the proxy
#: refuses as too large (413) is halved and re-sent.
PUSH_BATCH_ROWS: int = 1_000

#: Changelog entries per ``GET /pull``. Above the hub's own default (500) for
#: the same reason as the push; below
#: :data:`apps.sync_hub.identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS`
#: (2,000), so one page of tracks never elects the whole library, and within
#: the hub's ``MAX_PULL_LIMIT``.
PULL_LIMIT: int = 1_000

#: Total push -> pull rounds one ``run_sync`` will run before it gives up
#: settling. One initial round plus up to two SETTLE rounds (round 3 finding
#: R6): a digest mismatch that the fence still explains as movement re-runs
#: the round trip until the fence stops moving, and only a mismatch that
#: OUTLASTS the bound while still moving raises :class:`SyncStillMoving`.
#: Round 3 settled exactly once, so a fleet writing across two round trips
#: raised the corruption alarm on ordinary concurrency.
MAX_ROUNDS: int = 3

#: Thread-name prefix of the worker :func:`_digests` fetches the hub digest
#: on, so a leaked worker is findable by name.
DIGEST_WORKER_PREFIX: str = "cloudsync-hub-digest"

#: ``hub machine id -> (local overall, hub overall)`` of the inconclusive
#: comparison this process last reported at ERROR. The scheduler re-runs the
#: same comparison every tick; only a changed digest pair is news.
_reported_inconclusive: dict[str, tuple[str, str]] = {}


class SyncDigestMismatch(RuntimeError):
    """Spoke and hub disagree after a sync. No repair is attempted."""

    def __init__(
        self,
        message: str,
        *,
        diff: tuple[digest_diff.DigestDiffRow, ...] = (),
        digests: tuple[protocol.SyncDigest, protocol.SyncDigest] | None = None,
    ) -> None:
        super().__init__(message)
        self.diff = diff
        #: The (local, hub) digests that disagreed. ``None`` for a halt that is
        #: not a digest comparison (a refused push), which recovery then
        #: cannot attribute.
        self.digests = digests
        #: Tables whose digests differed; empty when ``digests`` is ``None``.
        self.divergent: tuple[str, ...] = (
            () if digests is None else digests[0].divergent_tables(digests[1])
        )


class SyncStillMoving(RuntimeError):
    """The fleet kept writing across the settle bound. NOT corruption.

    Distinct from :class:`SyncDigestMismatch` on purpose (round 3 finding
    R6): "another machine is still writing" and "the merge is wrong" must
    never share a message, or the one alarm ADR 04 c6 reserves for corruption
    stops being trustworthy. A caller retries this when the fleet is quiet.
    """


class SyncVersionMismatch(RuntimeError):
    """The hub speaks a different ``WIRE_VERSION`` (or, pre-split, schema).

    See :mod:`apps.sync_hub.wire_version`: a hub on another SCHEMA_VERSION
    but the same wire version is NOT a mismatch.
    """


#: Engine-side failures re-exported onto the client surface so a caller of
#: :func:`run_sync` can catch them without importing the engine and protocol
#: modules (round 3 finding R7 / R4's engine-side raise-contract half).
#: :class:`SyncApplyError` is a row the merge refused. Round 5 narrowed
#: :class:`SyncProtocolError` to a value arriving OFF THE WIRE, or a local row
#: whose defect is not a stamp (a BLOB column, a column-count mismatch): a
#: STORED ``updated_at`` that cannot be ordered no longer raises here at all,
#: it quarantines that row (see :attr:`SyncResult.quarantined`). Both are part
#: of ``run_sync``'s declared contract and propagate as themselves.
SyncApplyError = engine.SyncApplyError
SyncProtocolError = protocol.SyncProtocolError


# ----- one round -------------------------------------------------------------


def _undelivered(offer: engine.Offer, push: _PushOutcome) -> bool:
    """True when the peer decided fewer rows than this machine offered.

    The peer's own conservation law: ``accepted + rejected + quarantined``
    equals the rows offered, so ``accepted + rejected`` short of the offer
    means rows were given to it and never decided. Read this way rather than
    off ``push.hub_quarantined`` on purpose -- that counter is ``None`` when
    the peer did not report, and "did not report" is not "reported zero"
    (.claude/rules/verification.md). A shortfall is measured from numbers the
    peer has always sent, so it also catches a peer that drops rows for a
    reason this build has no counter for.
    """
    shortfall = len(offer.rows) - (push.accepted + push.rejected)
    if shortfall <= 0:
        return False
    log.error(
        "the hub decided only %d of the %d row(s) this machine offered "
        "(%d accepted, %d rejected, %s reported quarantined); holding the "
        "push fence at its previous value so every row in this window is "
        "offered again. The hub cannot re-deliver them -- it writes no "
        "hub_changelog entry for a row it refused -- so run `python -m "
        "apps.shared.state.normalize_stamps --live` ON THE HUB to clear it.",
        push.accepted + push.rejected,
        len(offer.rows),
        push.accepted,
        push.rejected,
        "no" if push.hub_quarantined is None else push.hub_quarantined,
    )
    return True


def _one_round(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    watermark: engine.Watermark,
    hub_generation: str,
    *,
    timer: PhaseTimer | None = None,
    reseed: bool = False,
) -> tuple[_Round, engine.Watermark]:
    """Offer what the fence selects, drain the hub, record the watermark.

    The fence (:func:`apps.sync_hub.engine.local_seq`) is read BEFORE the
    rows are selected, so a local write that commits during the round trip
    lands above it and is offered next time rather than falling into the gap
    that lost rows in round 1 (finding 3).

    What it records afterwards is
    :func:`apps.sync_hub.engine.settled_push_seq`, not the ceiling (round 5
    gate B1): a row this machine held back, or one the hub refused, must
    still be BELOW the fence when the next sync selects, or it is lost. A
    FULL offer's held rows have no changelog entry for a fence to stop
    below, so they are re-logged here instead, and a full offer the hub did
    not take in full is not recorded as complete at all -- its rows can
    predate ``local_changelog``, and then only another full offer reaches
    them.
    """
    ceiling = engine.local_seq(conn)
    offer = engine.spoke_push(conn, watermark=watermark, ceiling=ceiling)
    _refuse_a_stale_remap_push(channel, conn, machine_id, watermark.peer, offer.rows)
    # ``reseed`` is True only on the first round after this sync saw the hub
    # go backwards; a restored hub forgot rows it held, so nothing is stale.
    client_stale_copy.refuse_a_stale_copy_push(
        channel, machine_id, watermark.peer, offer.rows, reseed=reseed
    )
    with optional_span(timer, "push"):
        push = _push_in_batches(
            channel,
            machine_id,
            offer.rows,
            engine.machines_snapshot(conn),
            batch_rows=PUSH_BATCH_ROWS,
            reseed=reseed,
        )
    with optional_span(timer, "pull"):
        pull = _pull_in_chunks(
            channel,
            conn,
            machine_id,
            watermark.last_pull_seq,
            watermark.peer,
            limit=PULL_LIMIT,
        )
    was_full_offer = watermark.needs_full_offer
    # A refused push (apps.sync_hub.client_refusal) holds the fence exactly
    # like a shortfall, without the shortfall's stamp-repair advice.
    undelivered = push.refused or _undelivered(offer, push)
    settled = engine.Watermark(
        peer=watermark.peer,
        last_push_seq=engine.settled_push_seq(
            previous=watermark.last_push_seq,
            ceiling=ceiling,
            held_seq=offer.held_seq,
            undelivered=undelivered,
        ),
        last_pull_seq=pull.seq,
        # A full offer the hub did not take in full has NOT completed, and
        # ``needs_full_offer`` reads exactly this field. Its rows can predate
        # ``local_changelog``, so a fenced selection would never name the
        # refused ones again and the fence has no seq to stop below.
        last_sync_at=(
            None if (was_full_offer and undelivered) else sync_stamp.canonical_now()
        ),
        peer_generation=hub_generation,
    )
    relogged = 0
    confirmed = 0
    repairs: list[IdentityRepairRequest] = []
    with _transaction(conn):
        if push.identity_rejects:
            _, from_push = apply_hub_identity_rejects(conn, push.identity_rejects)
            repairs.extend(from_push)
        repairs.extend(pull.identity_repairs)
        if repairs:
            confirmed, _decided = _run_identity_repair(
                channel,
                conn,
                machine_id,
                watermark.peer,
                engine.machines_snapshot(conn),
                repairs,
                in_transaction=True,
            )
        if was_full_offer and offer.held:
            held_to_relog = (
                engine.still_held_rows(conn, offer.held)
                if repairs
                else offer.held
            )
            if held_to_relog:
                relogged = engine.relog_held(conn, held_to_relog, machine_id)
        engine.write_watermark(conn, settled)
    return (
        _Round(
            push=push,
            pull=pull,
            pushed=len(offer.rows),
            ceiling=ceiling,
            quarantined=offer.quarantined,
            relogged=relogged,
            identity_repairs=confirmed,
        ),
        settled,
    )


def _log_hub_build(hub_machine_id: str, advertised: object) -> None:
    """Say once per sync whether the hub understands a partial answer.

    Nothing is gated on this -- an un-upgraded hub protects itself by
    REFUSING (422), and that refusal propagates out of ``run_sync`` before
    any watermark is written, so this spoke loses nothing either way. It is
    logged because a fleet mid-rollout otherwise gives an operator no way to
    tell "the hub is old" from "the hub is broken", and the two have
    different fixes.

    ``advertised`` is deliberately untyped: an older hub sends no field at
    all, and that is UNKNOWN rather than an empty capability set.
    """
    if advertised is None:
        log.warning(
            "hub %s advertises no protocol capabilities, so it is on a build "
            "before quarantine/v1: it will REFUSE (422) any push, pull or "
            "digest it cannot answer in full, rather than holding one row "
            "back. Nothing is lost by that -- a refused push leaves this "
            "machine's fence where it was -- but the sync will not complete "
            "until that hub is repaired or upgraded.",
            hub_machine_id,
        )
        return
    caps = advertised if isinstance(advertised, list) else []
    if not capabilities.understands_quarantine(caps):
        log.warning(
            "hub %s advertises %r, which does not include %r; it will refuse "
            "rather than answer partially.",
            hub_machine_id,
            advertised,
            capabilities.QUARANTINE_V1,
        )
    if caps and not capabilities.understands_hash_pending(caps):
        log.warning(
            "hub %s advertises %r, which does not include %r; it will REFUSE "
            "(422) any push carrying hash_pending rows. Upgrade the hub before "
            "retrying.",
            hub_machine_id,
            advertised,
            capabilities.HASH_PENDING_V1,
        )


def _digests(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    *,
    timer: PhaseTimer | None = None,
) -> tuple[protocol.SyncDigest, protocol.SyncDigest]:
    """This machine's digest and the hub's, in that order.

    The local one is computed inside one read transaction so it describes a
    single snapshot rather than several (ADR 08 point 6b). ``GET /digest``
    now requires registration like every other endpoint (round 3 finding
    R8), so ``machine_id`` travels on every call.

    The two run at the same time (LIBM-120 L6 round 5): each is a read of its
    own database, on its own machine, and neither depends on the other, so
    waiting for one before starting the other cost the sum, about 2 s + 2 s
    at 10,000 tracks, where the larger is enough. The hub request goes on a
    worker thread, which waits on the socket and releases the GIL, while this
    thread hashes the library on ``conn``, which never leaves it. Both
    failures propagate as themselves, and leaving the ``with`` joins the
    worker either way. ``local_digest`` times the spoke's hash on ``conn``;
    ``digest`` times the hub ``GET /digest`` fetch on the worker thread.
    The two spans are siblings and may overlap.
    """
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix=DIGEST_WORKER_PREFIX) as pool:
        remote = pool.submit(_timed_hub_digest, channel, machine_id, timer)
        with _transaction(conn):
            with optional_span(timer, "local_digest"):
                local = protocol.sync_digest(conn)
        return local, remote.result()


def _timed_hub_digest(
    channel: HubTransport, machine_id: str, timer: PhaseTimer | None
) -> protocol.SyncDigest:
    with optional_span(timer, "digest"):
        return _fetch_hub_digest(channel, machine_id)


def _still_moving(
    conn: sqlite3.Connection, round_: _Round, remote: protocol.SyncDigest
) -> str | None:
    """Why this sync was not comparing two settled states, or None.

    Two ways a mismatch means "not finished yet" rather than "corrupt":

    * the hub's digest is taken at a seq ABOVE the one this spoke pulled to,
      so a third machine pushed during the round trip (round 2 finding 6b);
    * this machine's own changelog advanced past the push fence, so a local
      write landed mid-round-trip (round 2 finding N7).

    Both used to raise ``SyncDigestMismatch``, which ADR 04 c6 defines as
    corruption -- a corruption alarm that cleared itself on the next sync,
    which is exactly how a trustworthy signal stops being trusted.
    """
    if remote.seq > round_.pull.seq:
        return (
            f"the hub digest is taken at seq {remote.seq}, above the "
            f"{round_.pull.seq} this machine pulled to: another machine "
            f"pushed during the round trip"
        )
    # The round's own re-log (:func:`apps.sync_hub.engine.relog_held`) also
    # advances local_seq, and it is bookkeeping this sync wrote deliberately,
    # not evidence of a concurrent writer. Compared against ceiling PLUS the
    # entries this round wrote rather than against a seq re-read afterwards:
    # re-reading would swallow a genuine mid-sync write into the baseline and
    # turn ordinary concurrency into the ADR 04 c6 corruption alarm.
    settled_at = round_.ceiling + round_.relogged
    local_now = engine.local_seq(conn)
    if local_now > settled_at:
        return (
            f"this machine's changelog advanced from {settled_at} to "
            f"{local_now} during the round trip: a local write landed "
            f"mid-sync"
        )
    return None


# ----- settling ---------------------------------------------------------------


def _report_inconclusive(
    hub_machine_id: str, divergent: list[str], digest_pair: tuple[str, str]
) -> None:
    """ERROR once per hub per digest pair; an unchanged pair is one INFO line."""
    if _reported_inconclusive.get(hub_machine_id) == digest_pair:
        log.info(
            "digest still differs from hub %s in tables %s, the same "
            "inconclusive (quarantine) comparison this process reported at "
            "ERROR; not repeated.",
            hub_machine_id,
            divergent,
        )
        return
    _reported_inconclusive[hub_machine_id] = digest_pair
    log.error(
        "digest differs from hub %s in tables %s, and every one of "
        "them EXCLUDED rows from the comparison (quarantine). The "
        "comparison is inconclusive, not divergent, so this is not "
        "the ADR 04 c6 alarm. See engine-warn.log for held rows and "
        "the remedy per hold reason (normalize_stamps only when stamp "
        "faults are present). If the repair ran on the HUB, run "
        "`python -m apps.sync_hub rotate --data-dir DIR` there before "
        "syncing again: this machine may have already pulled past the "
        "changelog entries those rows were skipped in.",
        hub_machine_id,
        divergent,
    )


def _settle_digest(
    channel: HubTransport,
    conn: sqlite3.Connection,
    hub_machine_id: str,
    machine_id: str,
    hub_generation: str,
    watermark: engine.Watermark,
    rounds: list[_Round],
    digests: tuple[protocol.SyncDigest, protocol.SyncDigest],
    *,
    timer: PhaseTimer | None = None,
) -> tuple[list[_Round], engine.Watermark, protocol.SyncDigest, protocol.SyncDigest]:
    """Settle until the fence stops moving, bounded by :data:`MAX_ROUNDS`.

    Split out of :func:`run_sync` to keep its own branch count under the
    quality-gate mccabe limit (round 3 finding R6); ``digests`` is a pair
    rather than two more parameters for the same reason (ruff PLR0913/17).
    Not a repair pass: the same protocol runs each time and the same digest
    has to agree afterwards. What the loop removes is the false alarm --
    only a disagreement the fence CANNOT explain as in-flight writing is
    divergence, and a fleet that never stops writing gets its own error
    rather than the corruption one. ``rounds`` is mutated in place (and
    returned) so the caller's tally keeps growing across the whole sync.
    """
    local_digest, remote_digest = digests
    # A refused push leaves this machine holding rows the hub was not allowed
    # to take, so the two digests CANNOT agree and comparing them would raise
    # the corruption alarm on a billing state. The result says push_refused.
    while not rounds[-1].push.refused and local_digest.overall != remote_digest.overall:
        moving = _still_moving(conn, rounds[-1], remote_digest)
        if moving is None:
            divergent = local_digest.divergent_tables(remote_digest)
            excluded = local_digest.inconclusive_tables(remote_digest)
            if set(excluded) == set(divergent):
                _report_inconclusive(
                    hub_machine_id,
                    list(divergent),
                    (local_digest.overall, remote_digest.overall),
                )
                break
            samples = digest_diff.sample_divergence(
                channel,
                conn,
                machine_id,
                divergent,
                limit=digest_diff.DEFAULT_SAMPLE_LIMIT,
            )
            raise SyncDigestMismatch(
                digest_diff.format_mismatch_message(
                    hub_machine_id=hub_machine_id,
                    rounds=len(rounds),
                    divergent=divergent,
                    local_overall=local_digest.overall,
                    remote_overall=remote_digest.overall,
                    diffs=samples,
                ),
                diff=tuple(samples),
                digests=(local_digest, remote_digest),
            )
        if len(rounds) >= MAX_ROUNDS:
            raise SyncStillMoving(
                f"could not compare two settled states against hub "
                f"{hub_machine_id} within {MAX_ROUNDS} rounds: {moving}. "
                f"This is concurrent writing, not divergence; retry when "
                f"the fleet is quiescent."
            )
        log.info(
            "digest differs from hub %s but %s; settling with another "
            "round (%d run so far) before calling it divergence.",
            hub_machine_id,
            moving,
            len(rounds),
        )
        nxt, watermark = _one_round(
            channel, conn, machine_id, watermark, hub_generation, timer=timer
        )
        rounds.append(nxt)
        local_digest, remote_digest = _digests(channel, conn, machine_id, timer=timer)
    return rounds, watermark, local_digest, remote_digest


def _refuse_a_stale_remap_push(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    hub_machine_id: str,
    rows: list[protocol.RowChange],
) -> None:
    """Halt BEFORE a push that would carry a stale remap's moved rows.

    Runs inside every :func:`_one_round` over the exact offer it is about to
    push. Recovery cannot help here: a row the remap moved onto the survivor
    is indistinguishable from the survivor's own, so once the hub accepts it
    no retirement can take it back. Nothing is pushed; the ADR 04 c6 alarm
    is raised with the pairs a human has to decide. A remap is only ever
    removed together with the repair of its moved rows
    (:func:`apps.sync_hub.client_recovery.retire_stale_remaps`), so every
    persisted pair is still here to check.
    """
    spread = client_recovery.stale_remaps_the_offer_would_spread(
        channel, conn, machine_id, rows
    )
    if spread:
        raise SyncDigestMismatch(
            f"refusing to push to hub {hub_machine_id}: this sync's offer names "
            f"identity-remap survivor(s) whose remap the hub contradicts "
            f"(loser, survivor): {spread}. The remap moved the loser's rows onto "
            f"the survivor, so pushing would re-key them on the hub, and no "
            f"automatic recovery can tell a moved row from the survivor's own. "
            f"Nothing was pushed. No repair attempted; ADR 04 c6. Decide per row "
            f"whether it belongs to the loser or the survivor, then delete the "
            f"pair from sync_identity_remap."
        )


def _inconclusive_tracks_digests(
    settled: tuple[list[_Round], engine.Watermark, protocol.SyncDigest, protocol.SyncDigest],
) -> tuple[protocol.SyncDigest, protocol.SyncDigest] | None:
    """The (local, hub) digests of a settle accepted as INCONCLUSIVE on ``tracks``.

    A remap loser this spoke still stores is held out of its ``tracks``
    digest and counted as quarantined, while the hub hashes it. When the
    loser has no children to diverge, ``tracks`` is the only differing table
    and :func:`_settle_digest` accepts it as unmeasured instead of raising.
    A refused push is a billing state, not a digest, so it names nothing.
    """
    rounds, _watermark, local_digest, remote_digest = settled
    if rounds[-1].push.refused or local_digest.overall == remote_digest.overall:
        return None
    if "tracks" not in local_digest.inconclusive_tables(remote_digest):
        return None
    return local_digest, remote_digest


def _settle_or_recover(
    channel: HubTransport,
    conn: sqlite3.Connection,
    hub_machine_id: str,
    machine_id: str,
    hub_generation: str,
    watermark: engine.Watermark,
    rounds: list[_Round],
    *,
    timer: PhaseTimer | None = None,
) -> tuple[
    list[_Round], engine.Watermark, protocol.SyncDigest, protocol.SyncDigest, int
]:
    """Fresh digests, :func:`_settle_digest`, plus ONE stale-remap retirement.

    A settled mismatch, or a settle accepted as inconclusive on ``tracks``,
    asks :func:`apps.sync_hub.client_recovery.retire_stale_remaps` whether
    persisted identity remaps the hub contradicts explain every divergent
    row. If they do, it deletes them and applies the losers' hub bundles in
    the same write-locked transaction, so each loser and its children stand
    under their own ids again, and the state is settled once more against
    fresh digests: a mismatch there raises the ADR 04 c6 alarm. A divergence
    with any other cause changes nothing and the settle's own outcome
    stands. The fifth element is how many remaps were retired.
    """
    try:
        settled = _settle_digest(
            channel, conn, hub_machine_id, machine_id, hub_generation,
            watermark, rounds, _digests(channel, conn, machine_id, timer=timer),
            timer=timer,
        )
    except SyncDigestMismatch as mismatch:
        retired = client_recovery.retire_stale_remaps(
            channel, conn, machine_id, hub_machine_id, mismatch.digests
        )
        if not retired:
            # A library holding tracks the fleet dropped diverges on
            # ``tracks`` even when none of them was offered (a copy that
            # carried another machine's watermark). Name that cause and its
            # remedy instead of the bare alarm; any other cause still raises.
            client_stale_copy.raise_if_stale_library(
                channel, conn, machine_id, hub_machine_id, mismatch
            )
            raise
    else:
        retired = client_recovery.retire_stale_remaps(
            channel, conn, machine_id, hub_machine_id,
            _inconclusive_tracks_digests(settled),
        )
        if not retired:
            return (*settled, 0)
    settled = _settle_digest(
        channel, conn, hub_machine_id, machine_id, hub_generation,
        engine.read_watermark(conn, hub_machine_id), rounds,
        _digests(channel, conn, machine_id, timer=timer), timer=timer,
    )
    return (*settled, len(retired))


def run_sync(
    data_dir: Path,
    hub_url: str,
    *,
    transport: HubTransport | None = None,
    name: str | None = None,
) -> SyncResult:
    """Sync this machine's state DB against the hub, once.

    Declared failures, all propagating as themselves (round 3 finding R7):

    * :class:`SyncDigestMismatch` -- the two sides settled and still disagree.
      A hard stop: nothing here retries, repairs or reconciles a divergence,
      except the one retirement :func:`_settle_or_recover` runs after the
      hub has PROVEN a persisted identity remap stale and the remaps explain
      every divergent row.
    * :class:`SyncStillMoving` -- the fleet kept writing past the settle
      bound, so no two settled states could be compared. Concurrency, not
      corruption; retry when quiet.
    * :class:`SyncVersionMismatch` -- the hub speaks another ``WIRE_VERSION``.
    * :class:`SyncStaleTracks` -- the offer carries live tracks another
      machine wrote that the hub no longer holds: a stale or copied library
      (issue #4628). Raised before anything is pushed; the message names the
      ``stale-tracks`` remedy.
    * :class:`SyncTransportError` -- the hub was unreachable or answered with
      something unusable.
    * :class:`SyncApplyError` / :class:`SyncProtocolError` -- a row the merge
      refused, or a value that cannot be put on the wire. Raised engine-side
      while this spoke selects or applies rows. An unorderable STORED stamp
      is NOT one of these any more: it quarantines its own row and the sync
      completes (round 5). Live inferred-tier ``tracks`` rows with no
      ``content_hash`` and no normalizable ISRC are held the same way
      (CLOUDSYNC-07): they stay local so they cannot mint a path-tier PK,
      and identity-bearing rows still sync.

    A quarantine is reported, never raised: :attr:`SyncResult.quarantined`
    counts rows this machine could not offer, and the digest compares the
    SYNC-ELIGIBLE set, so two peers with identical eligible content converge
    even when one of them is holding a legacy row back.
    """
    channel: HubTransport = (
        transport
        if transport is not None
        else HttpTransport(
            hub_url,
            bearer=spoke_credential.read_credential(Path(data_dir)),
            push_timeout_s=PUSH_TIMEOUT_S,
        )
    )
    conn = state_db.open_rw(state_db_path(Path(data_dir)))
    timer = PhaseTimer()
    try:
        identity = machine_identity.register_machine(
            conn, data_dir=Path(data_dir), name=name
        )
        local_machine = _local_machine_row(conn, identity.machine_id)

        with timer.span("hello"):
            hello = channel.post(
                f"{API_PREFIX}/hello",
                {
                    "machine": local_machine.to_wire(),
                    "schema_version": state_schema.SCHEMA_VERSION,
                    "wire_version": wire_version.WIRE_VERSION,
                    "machines": [
                        machine.to_wire() for machine in engine.machines_snapshot(conn)
                    ],
                    "capabilities": list(capabilities.THIS_BUILD),
                },
            )
        # Absent means a pre-split hub, judged on schema; never read as "same".
        hub_wire = hello.get("wire_version")
        if hub_wire is not None and not isinstance(hub_wire, int):
            raise SyncTransportError("hello response carries a non-integer 'wire_version'")
        refusal = wire_version.incompatibility(
            hub_wire, _int_from(hello, "schema_version", "hello"), peer="hub"
        )
        if refusal is not None:
            raise SyncVersionMismatch(refusal.message)
        hub_machine_id = hello.get("hub_machine_id")
        if not isinstance(hub_machine_id, str) or not hub_machine_id:
            raise SyncTransportError("hello response lacks a hub_machine_id")
        # Same owner scoping as the pull merge: the hub authored the hello
        # snapshot, so this spoke's own row is never overwritten from it. That
        # is what stops a spoke re-poisoning itself on every sync (round 3 R1).
        engine.merge_machines(
            conn, _machines_from(hello, "hello"), caller_id=hub_machine_id
        )
        hub_seq = _int_from(hello, "seq", "hello")
        hub_generation = hello.get("hub_generation")
        if not isinstance(hub_generation, str) or not hub_generation:
            raise SyncTransportError("hello response lacks a hub_generation")
        _log_hub_build(hub_machine_id, hello.get("capabilities"))
        spoke_credential.log_hub_verdict(hub_machine_id, hello.get("credential"))
        spoke_credential.record_credential_notice(
            Path(data_dir), hub_machine_id, hello.get("credential")
        )

        watermark = engine.read_watermark(conn, hub_machine_id)
        watermark, restored = _watermark_after_hello(
            hub_machine_id, hub_seq, hub_generation, watermark
        )
        needs_full_offer_at_start = watermark.needs_full_offer
        # Hold-back, not preflight: unidentifiable inferred rows stay local
        # so they cannot mint a path-tier PK. Identity-bearing rows still
        # offer. Local content-identity duplicates remap their children onto
        # the LWW survivor before the offer so the digest matches the hub.
        # prepare_spoke_identity manages its own bounded per-batch
        # transactions (issue #3251); it must not be wrapped in one more
        # transaction here, or a 900+ row backlog is back to one giant one.
        # NFC twins collapse first: collapse_all owns its own transaction.
        twins_repaired = client_recovery.collapse_location_twins(conn)
        prepare_spoke_identity(conn)

        first, watermark = _one_round(
            channel,
            conn,
            identity.machine_id,
            watermark,
            hub_generation,
            timer=timer,
            reseed=restored,
        )
        rounds, watermark, local_digest, remote_digest, remaps_dropped = (
            _settle_or_recover(
                channel,
                conn,
                hub_machine_id,
                identity.machine_id,
                hub_generation,
                watermark,
                [first],
                timer=timer,
            )
        )

        result = _result_from_rounds(
            rounds,
            machine_id=identity.machine_id,
            hub_machine_id=hub_machine_id,
            local_digest=local_digest,
            remote_digest=remote_digest,
            restored=restored,
            hub_quarantined=remote_digest.quarantined_rows,
            # Derived, not threaded through four signatures: _settle_digest
            # returns with the two overalls unequal exactly when it accepted
            # the comparison as inconclusive.
            digest_inconclusive=local_digest.overall != remote_digest.overall,
        )
        timings = timer.finish(result, needs_full_offer_at_start=needs_full_offer_at_start)
        return dataclasses.replace(
            result,
            timings=timings,
            location_twins_repaired=twins_repaired,
            stale_identity_remaps_dropped=remaps_dropped,
        )
    finally:
        conn.close()


__all__ = [
    "API_PREFIX",
    "DEFAULT_TIMEOUT_S",
    "MAX_ROUNDS",
    "PULL_LIMIT",
    "PUSH_BATCH_ROWS",
    "PUSH_BODY_MAX_BYTES",
    "PUSH_TIMEOUT_S",
    "HttpTransport",
    "HubTransport",
    "SyncApplyError",
    "SyncDigestMismatch",
    "SyncProtocolError",
    "SyncResult",
    "SyncStaleTracks",
    "SyncStillMoving",
    "SyncTransportError",
    "SyncVersionMismatch",
    "run_sync",
    "state_db_path",
]
