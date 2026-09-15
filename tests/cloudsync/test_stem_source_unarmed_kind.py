"""Pure classifier tests for transient vs structural stem hydration unarmed.

[if] a stem hydration unarmed error is structural or transient [then] classify it for re-arm policy, [else stop].
"""
from __future__ import annotations

import pytest

from apps.cloud.stem_source import (
    STEM_HUB_AUTH_REFUSED,
    STEM_HUB_INDEX_FAILED,
    STEM_HUB_UNREACHABLE,
    classify_stem_hydration_unarmed,
)

pytestmark = pytest.mark.requirement("STEM-32")


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (STEM_HUB_AUTH_REFUSED, "hub refused sync credential", "structural"),
        (None, "boto3 is required for the R2 asset tier", "structural"),
        (
            None,
            "CloudSync is enabled but this machine has no stored sync credential; "
            "re-enroll with the hub before stem hydration can arm",
            "structural",
        ),
        (None, "sync credential file is world-readable", "structural"),
        (STEM_HUB_UNREACHABLE, "stem index refresh: connection refused", "transient"),
        (
            STEM_HUB_INDEX_FAILED,
            "stem index refresh: hub answered HTTP 403 (HOST_NOT_ALLOWED)",
            "transient",
        ),
        (
            STEM_HUB_INDEX_FAILED,
            "stem index refresh: hub answered HTTP 503 (upstream down)",
            "transient",
        ),
        (None, "timed out waiting for hub", "transient"),
    ],
)
def test_classify_stem_hydration_unarmed(code, message, expected) -> None:
    assert classify_stem_hydration_unarmed(code, message) == expected
