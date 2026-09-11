"""The LIFECYCLE of a paid entitlement: what happens between paying and lapsing.

Pure: no I/O, no clock, no database. A state machine over four states and a
closed set of events, plus the one question a server-side check point asks of
a state -- may this subject READ, and may it WRITE.

    active    --payment_failed-------> past_due
    past_due  --payment_recovered----> active
    past_due  --grace_expired--------> read_only
    active    --plan_ended-----------> read_only
    past_due  --plan_ended-----------> read_only
    read_only --read_only_expired----> archived
    read_only --resubscribed---------> active
    archived  --resubscribed---------> active

WHY THESE FOUR (specs/cloudsync-paid-tier.md).  The one rule a lapse must obey
is NEVER DESTROY A USER'S DATA.  A first implementation without explicit
states tends to refuse everything the moment a card fails, pull included, and
that strands a user's library on a server they can no longer read from.

  * ``active``    -- full service.
  * ``past_due``  -- full service, through the merchant's dunning window. A
    failed card is usually an expired card, not a decision to leave.
  * ``read_only`` -- the user can take everything OUT (pull, digest, status)
    and cannot put anything new IN (push). Their library is never hostage.
  * ``archived``  -- no service, but every row is RETAINED. Resubscribing
    restores it exactly, because nothing was removed.

THERE IS NO DELETE EVENT, on purpose.  Deletion is not a lifecycle outcome: it
is an explicit user act with a typed confirmation (the account API's
``ACCOUNT_DELETE_CONFIRM`` pattern), after notice.  A transition that removed
rows would, through ``users -> machine_owners ON DELETE CASCADE``, take a
machine's whole ownership record with it.
``tests/entitlements/test_lifecycle.py`` pins the vocabulary and
``tests/cloudsync/test_hosted_entitlement_gate.py`` walks every transition
against a real hub and counts every row.

GRACE LENGTHS ARE NOT HERE.  How long ``past_due`` and ``read_only`` last is a
pricing decision only the maintainer can make, so this module takes the EVENTS
(``grace_expired``, ``read_only_expired``) from whatever tracks the clock and
never carries a default window of its own.
"""

from __future__ import annotations

from typing import Literal, get_args

LifecycleState = Literal["active", "past_due", "read_only", "archived"]

LifecycleEvent = Literal[
    "payment_failed",
    "payment_recovered",
    "grace_expired",
    "plan_ended",
    "read_only_expired",
    "resubscribed",
]

#: What a check point is asking to do. ``read`` takes data out (pull,
#: digest); ``write`` puts data in (push).
Operation = Literal["read", "write"]

STATES: tuple[LifecycleState, ...] = get_args(LifecycleState)
EVENTS: tuple[LifecycleEvent, ...] = get_args(LifecycleEvent)
OPERATIONS: tuple[Operation, ...] = get_args(Operation)

#: The whole machine. A (state, event) pair absent here is an error, never a
#: no-op: an event arriving in a state it cannot apply to means the tracker
#: and this module disagree about where the subject is.
TRANSITIONS: dict[tuple[LifecycleState, LifecycleEvent], LifecycleState] = {
    ("active", "payment_failed"): "past_due",
    ("past_due", "payment_recovered"): "active",
    ("past_due", "grace_expired"): "read_only",
    ("active", "plan_ended"): "read_only",
    ("past_due", "plan_ended"): "read_only",
    ("read_only", "read_only_expired"): "archived",
    ("read_only", "resubscribed"): "active",
    ("archived", "resubscribed"): "active",
}

#: Which operations each state admits. ``archived`` admits none but, like
#: every state, retains the data.
_ALLOWED: dict[LifecycleState, frozenset[Operation]] = {
    "active": frozenset({"read", "write"}),
    "past_due": frozenset({"read", "write"}),
    "read_only": frozenset({"read"}),
    "archived": frozenset(),
}


class LifecycleTransitionError(ValueError):
    """An event was applied to a state it has no transition from."""


def _assert_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"unknown lifecycle state {state!r}; expected one of {list(STATES)}")


def transition(state: LifecycleState, event: LifecycleEvent) -> LifecycleState:
    """The state ``event`` moves ``state`` to. Raises on an undefined pair."""
    _assert_state(state)
    if event not in EVENTS:
        raise ValueError(f"unknown lifecycle event {event!r}; expected one of {list(EVENTS)}")
    target = TRANSITIONS.get((state, event))
    if target is None:
        raise LifecycleTransitionError(
            f"event {event!r} has no transition from state {state!r}. The "
            "tracker that emitted it and apps/entitlements/lifecycle.py "
            "disagree about where this subject is; reconcile the tracker "
            "rather than guessing a state."
        )
    return target


def allows(state: LifecycleState, operation: Operation) -> bool:
    """May a subject in ``state`` perform ``operation``?"""
    _assert_state(state)
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}; expected one of {list(OPERATIONS)}")
    return operation in _ALLOWED[state]


__all__ = [
    "EVENTS",
    "OPERATIONS",
    "STATES",
    "TRANSITIONS",
    "LifecycleEvent",
    "LifecycleState",
    "LifecycleTransitionError",
    "Operation",
    "allows",
    "transition",
]
