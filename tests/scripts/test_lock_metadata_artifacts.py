"""Artifact `size` and `upload-time` values as uv reads them (Codex P2 on #3763, round
55). Every row is a measurement on uv 0.8.17 against the six pair with the sdist's field
replaced and `uv lock --check --offline` run: a refused row is "Failed to parse
`uv.lock`", exit 2; a read row exits 0. `datetime.fromisoformat` had accepted a week
date uv refuses and refused basic forms and a `[UTC]` annotation uv reads; the size
check had no ceiling."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_shapes import ARTIFACTS
from tests.scripts.test_lock_metadata_types import UNMARKED_LOCK, UNMARKED_PYPROJECT

STAMP = '"2024-12-04T17:35:28.174Z"'


def _sdist(tmp_path: Path, old: str, new: str) -> tuple[int, str]:
    lock = UNMARKED_LOCK + ARTIFACTS
    assert lock.count(old) == 1, old
    return _run(tmp_path, UNMARKED_PYPROJECT, lock.replace(old, new))


@pytest.mark.parametrize(
    ("stamp", "named"),
    [
        ('"2024-W01-1T00:00:00+00:00"', "not a timestamp uv reads"),
        ('"2024-001T00:00:00Z"', "not a timestamp uv reads"),
        ('"2024-01-01T00:00:00"', "not a timestamp uv reads"),
        ('"2024-01-01T00:00:00.1234567890Z"', "not a timestamp uv reads"),
        ('"2024-01-01T00:00:00.Z"', "not a timestamp uv reads"),
        ('"2024-01-01T00:00:00Z "', "not a timestamp uv reads"),
        ('" 2024-01-01T00:00:00Z"', "not a timestamp uv reads"),
        ('"2024-01-01T00:00:00 Z"', "not a timestamp uv reads"),
        ('"2024-1-01T00:00:00Z"', "not a timestamp uv reads"),
        ('"+2024-01-01T00:00:00Z"', "not a timestamp uv reads"),
        ('"-0001-01-01T00:00:00Z"', "not a timestamp uv reads"),
        ('"2024-01-01"', "not a timestamp uv reads"),
        ('"20240101T000000"', "not a timestamp uv reads"),
        ('"2024-01-01T24:00:00Z"', "not a time of day"),
        ('"2024-01-01T00:60:00Z"', "not a time of day"),
        ('"2024-01-01T00:00:61Z"', "not a time of day"),
        ('"2024-02-30T00:00:00Z"', "not a calendar date"),
        ('"2023-02-29T00:00:00Z"', "not a calendar date"),
        ('"2024-13-01T00:00:00Z"', "not a calendar date"),
        ('"2024-01-00T00:00:00Z"', "not a calendar date"),
        ('"2024-01-01T00:00:00+26:00"', "past 25:59:59"),
        ('"2024-01-01T00:00:00+25:59:60"', "past 25:59:59"),
        ('"9999-12-30T23:59:59Z"', "edge of uv's timestamp range"),
        ('"9999-12-31T00:00:00Z"', "edge of uv's timestamp range"),
        ('"+010000-01-01T00:00:00Z"', "edge of uv's timestamp range"),
        ("2024-01-01T00:00:00Z", "is not a string"),
    ],
    ids=[
        "week-date",
        "ordinal-date",
        "no-zone",
        "ten-fraction-digits",
        "empty-fraction",
        "trailing-space",
        "leading-space",
        "space-before-zone",
        "one-digit-month",
        "signed-four-digit-year",
        "negative-four-digit-year",
        "date-only",
        "basic-no-zone",
        "hour-24",
        "minute-60",
        "second-61",
        "february-30",
        "february-29-common-year",
        "month-13",
        "day-0",
        "offset-26h",
        "offset-second-60",
        "year-9999-late",
        "year-9999-last-day",
        "year-10000",
        "toml-datetime-literal",
    ],
)
def test_an_upload_time_uv_cannot_read_is_unknown(tmp_path: Path, stamp: str, named: str) -> None:
    """uv 0.8.17 exit 2 on each row but the last three, which sit at the edge of its
    timestamp range (it reads `9999-12-30T00:00:00Z` and refuses `9999-12-30T23:59:59Z`)
    and are UNKNOWN as not modeled, never clean"""
    code, message = _sdist(tmp_path, STAMP, stamp)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


@pytest.mark.parametrize(
    "stamp",
    [
        '"2024-01-01T00:00:00+00:00"',
        '"2024-01-01T00:00:00Z"',
        '"2024-01-01 00:00:00Z"',
        '"2024-01-01t00:00:00z"',
        '"2024-01-01T00:00:00.123456Z"',
        '"2024-01-01T00:00:00.123456789Z"',
        '"2024-01-01T00:00:00,5Z"',
        '"2024-01-01T00:00:60Z"',
        '"2024-01-01T00:00:00+0000"',
        '"2024-01-01T00:00:00+00"',
        '"2024-01-01T00:00:00-00:00"',
        '"2024-01-01T00:00:00+00:00:00"',
        '"2024-01-01T00:00:00+25:59"',
        '"2024-01-01T00:00:00-25:59:59"',
        '"2024-01-01T00:00Z"',
        '"2024-01-01T00Z"',
        '"20240101T000000Z"',
        '"20240101T0000Z"',
        '"20240101T000000.5Z"',
        '"20240101T00:00:00Z"',
        '"2024-01-01T000000Z"',
        '"2024-01-01T00:00:00Z[UTC]"',
        '"2024-01-01T00:00:00+00:00[Europe/London]"',
        '"2024-02-29T00:00:00Z"',
        '"0000-01-01T00:00:00+01:00"',
        '"0001-01-01T00:00:00Z"',
        '"+002024-01-01T00:00:00Z"',
        '"9999-12-30T00:00:00Z"',
    ],
    ids=[
        "offset-colon",
        "zulu",
        "space-separator",
        "lowercase",
        "six-fraction-digits",
        "nine-fraction-digits",
        "comma-fraction",
        "leap-second",
        "offset-no-colon",
        "offset-hours-only",
        "offset-minus-zero",
        "offset-with-seconds",
        "offset-25-59",
        "offset-minus-25-59-59",
        "no-seconds",
        "hour-only",
        "basic",
        "basic-short-time",
        "basic-fraction",
        "basic-date-extended-time",
        "extended-date-basic-time",
        "utc-annotation",
        "zone-annotation",
        "february-29-leap-year",
        "year-0000",
        "year-0001",
        "six-digit-year",
        "year-9999-first-day",
    ],
)
def test_an_upload_time_uv_reads_keeps_the_verdict(tmp_path: Path, stamp: str) -> None:
    """CONTROLS: uv 0.8.17 exit 0 on each row (measured round 55), but the last, which
    this check leaves UNKNOWN as the edge of uv's range rather than reject; none is a
    rejection"""
    code, message = _sdist(tmp_path, STAMP, stamp)
    if stamp == '"9999-12-30T00:00:00Z"':
        assert code == EXIT_UNKNOWN and "not modeled" in message, message
        return
    assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("size", "code", "named"),
    [
        ("9223372036854775807", EXIT_OK, ""),
        ("4294967296", EXIT_OK, ""),
        ("0", EXIT_OK, ""),
        ("-0", EXIT_OK, ""),
        ("0x10", EXIT_OK, ""),
        ("+5", EXIT_OK, ""),
        ("1_000", EXIT_OK, ""),
        ("18446744073709551615", EXIT_UNKNOWN, "exceeds a signed 64-bit integer; uv 0.7.22"),
        ("9223372036854775808", EXIT_UNKNOWN, "exceeds a signed 64-bit integer; uv 0.7.22"),
        ("18446744073709551616", EXIT_UNKNOWN, "exceeds 64 bits; uv rejects the file"),
        ("1.0", EXIT_UNKNOWN, "not a byte count"),
        ("1e3", EXIT_UNKNOWN, "not a byte count"),
    ],
    ids=[
        "i64-max",
        "above-u32",
        "zero",
        "negative-zero",
        "hex",
        "plus-sign",
        "underscore",
        "u64-max",
        "i64-max-plus-one",
        "u64-max-plus-one",
        "float",
        "exponent",
    ],
)
def test_an_artifact_size_is_read_up_to_the_ceiling_uv_reads(
    tmp_path: Path, size: str, code: int, named: str
) -> None:
    """uv 0.8.17 reads a size up to 2**64-1 and refuses 2**64 and any float (exit 2,
    measured round 55); uv 0.7.22 stops at 2**63-1 (Codex), so a size between the two
    ceilings is UNKNOWN rather than certified for either uv"""
    got, message = _sdist(tmp_path, "size = 34031", f"size = {size}")
    assert got == code, (size, message)
    assert named in message, message
