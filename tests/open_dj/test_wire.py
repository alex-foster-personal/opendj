"""Tests for :mod:`apps.open_dj.wire` helpers.

[if] a cue color is present [then] invalid RGB is rejected, [else stop].
"""
from __future__ import annotations

import pytest

from apps.open_dj.wire import color_hex_to_int

pytestmark = pytest.mark.requirement("OPEN-8212")

_CONTRACT = r"6-digit hex RGB|0\.\.0xffffff"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("#ff8800", 0xFF8800),
        ("FF8800", 0xFF8800),
        ("#AbCdEf", 0xABCDEF),
        (0, 0),
        (0xFFFFFF, 0xFFFFFF),
    ],
)
def test_color_hex_to_int_accepts_valid_present_values(value: object, expected: int) -> None:
    assert color_hex_to_int(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ff",
        "ff880",
        "ff88000",
        "gg8800",
        " #ff8800",
        "ff8800 ",
        "-1",
        "0xff8800",
        "ff_8800",
        -1,
        0x1000000,
        True,
        False,
        None,
        [],
        3.14,
    ],
)
def test_color_hex_to_int_rejects_malformed_present_values(value: object) -> None:
    with pytest.raises(ValueError, match=_CONTRACT) as excinfo:
        color_hex_to_int(value)
    assert repr(value) in str(excinfo.value)
