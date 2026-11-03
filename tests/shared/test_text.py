"""Shared text coercion for scanners and CloudSync identity.

[if] value is None or strips to empty [then] as_text returns None, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.text import as_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  a  ", "a"),
        (42, "42"),
        (b"x", "b'x'"),
    ],
)
def test_as_text(value: object, expected: str | None) -> None:
    assert as_text(value) == expected
