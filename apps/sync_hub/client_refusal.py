"""The ONE hub refusal a spoke survives: a plan that does not admit writes.

A hosted hub (``apps/sync_hub/entitlement_gate.py``) refuses ``POST /push``
with 403 ``entitlement_not_in_plan`` when the calling machine's owner is
``read_only``, and keeps ``GET /pull`` open, because ``read_only`` exists so
a lapsed user can always get their library OUT. The spoke pushes before it
pulls, so if that refusal propagated like every other transport error, a
spoke with a single unpushed edit -- which is every DJ's normal state --
would never reach the pull, and the promise would hold only for a hand-built
HTTP GET. So the push loop stops on this refusal and the round goes on to
pull; every other refusal (an unowned machine, an unreachable hub, an
archived owner's pull) still raises, exactly as before.

Nothing about the refused rows is lost. The fence is held where it was
(:func:`apps.sync_hub.engine.settled_push_seq` with ``undelivered``), so
every refused row is offered again on the next sync, and the hub keeps every
row it already held (the lifecycle has no delete).
"""

from __future__ import annotations

import logging

from apps.shared.wire_codes import NOT_IN_PLAN_CODE
from apps.sync_hub.transport import SyncTransportError

log = logging.getLogger("apps.sync_hub.client")

#: The status a plan refusal arrives with. Paired with the code, never alone:
#: a 403 with any other code (``SYNC_HOSTED_UNOWNED``) is not survivable.
PLAN_REFUSAL_STATUS: int = 403


def is_plan_refusal(exc: SyncTransportError) -> bool:
    """True only for the hub's declared 403 ``entitlement_not_in_plan``."""
    return exc.status_code == PLAN_REFUSAL_STATUS and exc.code == NOT_IN_PLAN_CODE


def log_push_refused(exc: SyncTransportError, *, unsent: int) -> None:
    """Say loudly that this machine's edits did not leave it, and why."""
    log.warning(
        "the hub REFUSED this machine's push (%s): the owner's plan does not "
        "admit writes, so %d row(s) stay on this machine only and are offered "
        "again on the next sync. Pull still runs, so rows the hub holds keep "
        "arriving here. Hub answer: %s",
        NOT_IN_PLAN_CODE,
        unsent,
        exc,
    )


__all__ = ["PLAN_REFUSAL_STATUS", "is_plan_refusal", "log_push_refused"]
