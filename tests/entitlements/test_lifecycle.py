"""The paid-entitlement lifecycle: a lapse never takes a user's data with it.

The row-level proof that no transition deletes anything runs against a real
hub in ``tests/cloudsync/test_hosted_entitlement_gate.py``; this module pins
the pure machine those rows depend on.

- [if] read_only admits a write, or refuses a read [then] broken, [else stop].
- [if] any state is terminal with no path back to active [then] broken, [else stop].
- [if] an undefined (state, event) pair is silently accepted [then] broken, [else stop].
- [if] the event vocabulary grows a deletion event [then] broken, [else stop].
"""

from __future__ import annotations

import pytest

from apps.entitlements import lifecycle
from apps.entitlements.lifecycle import (
    EVENTS,
    STATES,
    TRANSITIONS,
    LifecycleTransitionError,
    allows,
    transition,
)

pytestmark = pytest.mark.requirement("CAT-04")


def test_the_four_states_are_exactly_the_agreed_ones() -> None:
    """If a state is added or renamed without the spec then broken."""
    assert STATES == ("active", "past_due", "read_only", "archived")


@pytest.mark.parametrize(
    ("state", "read", "write"),
    [
        ("active", True, True),
        ("past_due", True, True),
        ("read_only", True, False),
        ("archived", False, False),
    ],
)
def test_each_state_admits_exactly_its_operations(
    state: lifecycle.LifecycleState, read: bool, write: bool
) -> None:
    """If read_only stops admitting pull, or admits push, then broken."""
    assert allows(state, "read") is read
    assert allows(state, "write") is write


def test_every_transition_lands_on_a_known_state() -> None:
    """If a transition targets a state outside STATES then broken."""
    for (source, event), target in TRANSITIONS.items():
        assert source in STATES and event in EVENTS and target in STATES
        assert transition(source, event) == target


def test_every_state_can_get_back_to_active() -> None:
    """If a state is terminal then resubscribing cannot restore a lapsed user's data."""
    for start in STATES:
        seen = {start}
        frontier = [start]
        while frontier:
            here = frontier.pop()
            for (source, _event), target in TRANSITIONS.items():
                if source == here and target not in seen:
                    seen.add(target)
                    frontier.append(target)
        assert "active" in seen, f"{start} can never return to active"


def test_the_lapse_path_passes_through_read_only_before_archived() -> None:
    """If a lapse jumps straight from paying to no-access then a user loses the chance to pull."""
    for (source, _event), target in TRANSITIONS.items():
        if target == "archived":
            assert source == "read_only", (source, target)


def test_an_undefined_pair_raises_rather_than_holding_state() -> None:
    """If an event that cannot apply is silently a no-op then broken."""
    with pytest.raises(LifecycleTransitionError):
        transition("archived", "payment_failed")
    with pytest.raises(LifecycleTransitionError):
        transition("active", "resubscribed")


def test_unknown_names_are_refused() -> None:
    """If an unknown state, event or operation is answered then broken."""
    with pytest.raises(ValueError):
        transition("cancelled", "resubscribed")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        transition("active", "refund")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        allows("active", "delete")  # type: ignore[arg-type]


def test_no_event_or_state_names_a_deletion() -> None:
    """If the lifecycle vocabulary can express deleting data then a lapse could do it."""
    forbidden = ("delete", "purge", "erase", "destroy", "wipe", "drop")
    for name in (*STATES, *EVENTS):
        assert not any(word in name for word in forbidden), name
