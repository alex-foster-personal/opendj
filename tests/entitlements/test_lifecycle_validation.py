"""Pins lifecycle validation error messages for the shared _assert_member helper.

  - [if] a validation message drifts [then] this module fails, [else stop].
"""

from __future__ import annotations

import pytest

from apps.entitlements.lifecycle import allows, transition

pytestmark = pytest.mark.requirement("CAT-04")


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            lambda: transition("cancelled", "resubscribed"),
            "unknown lifecycle state 'cancelled'; expected one of "
            "['active', 'past_due', 'read_only', 'archived']",
        ),
        (
            lambda: transition("active", "refund"),
            "unknown lifecycle event 'refund'; expected one of "
            "['payment_failed', 'payment_recovered', 'grace_expired', "
            "'plan_ended', 'read_only_expired', 'resubscribed']",
        ),
        (
            lambda: allows("active", "delete"),
            "unknown operation 'delete'; expected one of ['read', 'write']",
        ),
    ],
)
def test_unknown_vocabulary_raises_with_expected_wording(call, expected: str) -> None:
    """[if] a message loses its value or choices [then] operators lose the hint, [else stop]."""
    with pytest.raises(ValueError) as caught:
        call()  # type: ignore[call-arg]
    assert str(caught.value) == expected
