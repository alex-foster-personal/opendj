"""Unit coverage for apps.diagnostics.probe_types helpers."""

from __future__ import annotations

import importlib
import re
from datetime import datetime, timedelta

import pytest

from apps.diagnostics.probe_types import (
    MIB,
    parse_byte_count,
    round_mb,
    utc_now,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1024", 1024),
        ("1K", 1024),
        ("1.5M", int(1.5 * MIB)),
        ("2G", 2 * 1024 * MIB),
        ("  512  ", 512),
        ("10k", 10 * 1024),
    ],
)
def test_parse_byte_count_accepts_common_units(raw: str, expected: int) -> None:
    assert parse_byte_count(raw) == expected


@pytest.mark.parametrize("raw", ["", "nope", "1X", "M1"])
def test_parse_byte_count_rejects_unparseable(raw: str) -> None:
    assert parse_byte_count(raw) is None


def test_round_mb_matches_mib_division() -> None:
    assert round_mb(MIB) == 1.0
    assert round_mb(MIB // 2) == 0.5


def test_utc_now_is_millisecond_zulu() -> None:
    stamp = utc_now()
    assert stamp.endswith("Z")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp)
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    offset = parsed.utcoffset()
    assert offset == timedelta(0)


@pytest.mark.parametrize(
    ("implementation", "facade"),
    [
        ("apps.diagnostics.probe_types", "scripts.diagnostics.probe_types"),
        (
            "apps.diagnostics.probe_native_metrics",
            "scripts.diagnostics.probe_native_metrics",
        ),
    ],
)
def test_scripts_facades_reexport_every_public_name(implementation: str, facade: str) -> None:
    impl = importlib.import_module(implementation)
    fac = importlib.import_module(facade)
    impl_public = {name for name in dir(impl) if not name.startswith("_")}
    missing = sorted(name for name in impl_public if not hasattr(fac, name))
    assert not missing, f"{facade} missing re-exports: {missing}"
