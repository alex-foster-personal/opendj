"""What a sync's rounds record, where the first one starts, and their sum.

Split out of :mod:`apps.sync_hub.client` (quality-gate file_size ratchet,
round 5 gate B1, which added the fence bookkeeping that pushed the module
over). Everything here is a pure function or a frozen record over the engine
and protocol types: nothing in this module opens a connection or touches the
wire, which is exactly what makes it separable from the round trip itself.

``apps.sync_hub.client`` re-exports these names, so an existing
``client._Round`` reference keeps working.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.sync_hub import engine, protocol
from apps.sync_hub.client_result import SyncResult
from apps.sync_hub.client_transport_ops import _PullOutcome, _PushOutcome

if TYPE_CHECKING:
    from apps.sync_hub.rejected_rows import RejectedRow

log = logging.getLogger("apps.sync_hub.client")


@dataclass(frozen=True)
class _Round:
    """One push -> pull pass, and the fence it ran against."""

    push: _PushOutcome
    pull: _PullOutcome
    pushed: int
    ceiling: int
    #: Rows the fence selected but this machine could not put on the wire.
    quarantined: int = 0
    #: ``local_changelog`` entries THIS round wrote to re-queue rows a full
    #: offer held back (:func:`apps.sync_hub.engine.relog_held`). Counted so
    #: :func:`_still_moving` can tell this machine's own bookkeeping from a
    #: concurrent local write, which look identical in ``local_seq`` alone.
    relogged: int = 0


def _watermark_after_hello(
    hub_machine_id: str,
    hub_seq: int,
    hub_generation: str,
    watermark: engine.Watermark,
) -> tuple[engine.Watermark, bool]:
    """The watermark to sync against, and whether a hub restore was detected.

    Split out of :func:`run_sync` to keep its own branch count under the
    quality-gate mccabe limit. Two ways the hub can have moved backwards:

    * **The generation token changed** (ADR 08 point 4 / round 1 finding A3,
      re-keyed for round 2 finding N6). The hub mints a new token when its DB
      moved backwards under a data dir that did not
      (:mod:`apps.sync_hub.generation`), so the hub was restored from a point
      in time and has forgotten rows this spoke already counted as
      delivered.
    * **The token is unchanged but the hub's seq sits BELOW what this spoke
      already pulled** (round 3 finding R2). That is the whole-data-dir
      restore the token cannot see: a Litestream DB restore rotates the
      token, but a restore that rolled the DATA DIR back rolls the anchor
      back with the DB, so the token agrees with the DB and nothing detects
      it. The sanctioned prune cannot produce it either -- it never drops
      the newest entry, so ``MAX(seq)`` cannot fall.

    Either way, nothing below the stale floor would ever be offered again,
    so both floors reset to zero and the whole library is re-offered; LWW
    rejects on equality, so the replay is idempotent (three replays deep in
    round 1).
    """
    generation_changed = (
        watermark.peer_generation is not None
        and watermark.peer_generation != hub_generation
    )
    seq_regressed = not generation_changed and watermark.last_pull_seq > hub_seq
    restored = generation_changed or seq_regressed
    if generation_changed:
        log.error(
            "hub %s reports generation %s but this machine last synced "
            "against %s; the hub went BACKWARDS (restored from a "
            "Litestream point in time?). Resetting both sync floors to 0 "
            "and re-offering the entire library against it.",
            hub_machine_id,
            hub_generation,
            watermark.peer_generation,
        )
        return engine.Watermark(peer=hub_machine_id), restored
    if seq_regressed:
        log.error(
            "hub %s reports max changelog seq %d under an UNCHANGED "
            "generation %s, below the %d this machine had pulled; the hub "
            "went backwards without rotating its token (whole-data-dir "
            "restore?). Resetting both sync floors to 0 and re-offering "
            "the entire library against it.",
            hub_machine_id,
            hub_seq,
            hub_generation,
            watermark.last_pull_seq,
        )
        return engine.Watermark(peer=hub_machine_id), restored
    return watermark, restored


def _refusal_verdicts(rounds: list[_Round], digest_inconclusive: bool) -> tuple[bool, bool]:
    """``(push_refused, digest_inconclusive)`` for the result, refusal first.

    A refused push ends the settle loop, so it can only be the LAST round;
    ``any`` is used anyway so the result cannot hide one. A refused sync's
    digests differ by construction, which is not "inconclusive" (that means
    quarantine), so the refusal owns the verdict. Split out of
    :func:`_result_from_rounds` to keep it under the quality-gate CC limit.
    """
    push_refused = any(round_.push.refused for round_ in rounds)
    return push_refused, digest_inconclusive and not push_refused


def _rejected_rows(rounds: list[_Round]) -> tuple[RejectedRow, ...]:
    """Every row the hub named as rejected, across all rounds (CLOUDSYNC-31).

    Split out of :func:`_result_from_rounds` to keep it under the quality-gate CC limit.
    """
    return tuple(row for round_ in rounds for row in round_.push.rejected_rows)


def _result_from_rounds(
    rounds: list[_Round],
    *,
    machine_id: str,
    hub_machine_id: str,
    local_digest: protocol.SyncDigest,
    remote_digest: protocol.SyncDigest,
    restored: bool,
    hub_quarantined: int | None,
    digest_inconclusive: bool,
) -> SyncResult:
    """Sum every round's counters into the one :class:`SyncResult`.

    Split out of :func:`run_sync` (whose mccabe count these generator
    expressions were pushing over the quality-gate limit), never for a reason
    of behavior: every count is still observed, none inferred.
    """
    push_refused, digest_inconclusive = _refusal_verdicts(rounds, digest_inconclusive)
    return SyncResult(
        machine_id=machine_id,
        hub_machine_id=hub_machine_id,
        pushed=sum(round_.pushed for round_ in rounds),
        accepted=sum(round_.push.accepted for round_ in rounds),
        rejected=sum(round_.push.rejected for round_ in rounds),
        rejected_rows=_rejected_rows(rounds),
        pulled=sum(round_.pull.pulled for round_ in rounds),
        applied=sum(round_.pull.applied for round_ in rounds),
        hub_seq=rounds[-1].pull.seq,
        digest=local_digest.overall,
        push_requests=sum(round_.push.requests for round_ in rounds),
        pull_requests=sum(round_.pull.requests for round_ in rounds),
        hub_restore_detected=restored,
        rounds=len(rounds),
        quarantined=sum(round_.quarantined for round_ in rounds),
        # Off the digest, not the rounds: the fence-scoped sum above reads 0
        # on a machine whose selection was empty, which says nothing about
        # how many rows are outside the sync set.
        quarantined_rows=local_digest.quarantined_rows or 0,
        quarantined_incoming=sum(round_.pull.quarantined for round_ in rounds),
        hub_quarantined=hub_quarantined,
        hash_pending=local_digest.hash_pending_rows,
        hub_hash_pending=remote_digest.hash_pending_rows,
        digest_inconclusive=digest_inconclusive,
        push_refused=push_refused,
    )


__all__ = [
    "_Round",
    "_result_from_rounds",
    "_watermark_after_hello",
]
