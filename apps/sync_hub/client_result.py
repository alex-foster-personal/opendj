"""The record one ``run_sync`` produces, and nothing else.

Split out of :mod:`apps.sync_hub.client` (quality-gate file_size ratchet,
round 5 final gate), the same way ``client_transport_ops``, ``engine_apply``
and ``protocol_common`` were split before it. Pure data: no imports from the
rest of this package, so the client imports it and never the other way round.
``apps.sync_hub.client`` re-exports :class:`SyncResult`, so every existing
``client.SyncResult`` reference keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncResult:
    """What one ``run_sync`` did. Every count is observed, none inferred."""

    machine_id: str
    hub_machine_id: str
    pushed: int
    accepted: int
    rejected: int
    pulled: int
    applied: int
    hub_seq: int
    digest: str
    #: HTTP requests this sync made to ``/push`` and ``/pull``. Observed, so
    #: a test can assert the batching actually batched rather than trusting
    #: that a constant was read.
    push_requests: int = 0
    pull_requests: int = 0
    #: True when the hub reported a generation token different from the one
    #: this spoke last synced against, so both floors were reset and the
    #: library re-offered.
    hub_restore_detected: bool = False
    #: push -> pull passes this sync made. 2 means the first comparison found
    #: the two sides still moving (a third machine pushed, or a local write
    #: landed mid-sync) and the sync settled instead of raising.
    rounds: int = 1
    #: Rows THIS SYNC'S FENCE selected and could not put on the wire, because
    #: a stored stamp on them cannot be ordered (round 5). Fence-scoped, so it
    #: reads 0 on a machine whose fence selected nothing while rows still sit
    #: outside the sync set; :attr:`quarantined_rows` is that number. Non-zero
    #: is not a failure -- every other row synced.
    quarantined: int = 0
    #: Rows outside this machine's sync set ALTOGETHER, off the local digest
    #: every sync computes. Fence-independent, which is why it is the number
    #: that belongs beside :attr:`digest_inconclusive`: "nothing held back,
    #: yet inconclusive" cannot be two readings of one machine. Cleared by
    #: ``python -m apps.shared.state.normalize_stamps --live``.
    quarantined_rows: int = 0
    #: Incoming rows this machine REFUSED because the local row they would be
    #: compared against cannot be ordered. Separate from ``quarantined``
    #: because they are different rows on different sides of the wire, and
    #: summing them would over-count a single legacy row.
    quarantined_incoming: int = 0
    #: True when the two digests differ ONLY in tables where one side held
    #: rows out of the comparison. Not divergence and not the ADR 04 c6
    #: alarm: an inconclusive measurement, which is a third answer and must
    #: not be folded into either of the other two.
    digest_inconclusive: bool = False
    #: Rows the HUB holds that are not in ITS sync set, read off the hub's
    #: own digest -- the one call every sync makes, so it is always measured.
    #: ``None`` means the hub did not report the field (an older build),
    #: which is surfaced as unknown and never coerced to 0.
    hub_quarantined: int | None = None


__all__ = ["SyncResult"]
