"""Tests for :mod:`apps.open_dj.provenance`.

[if] wrap() stamps any time but the caller's modified_at [then] fail, [else stop].
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.open_dj.provenance import utc_mtime, wrap

_FIXED = datetime(2026, 2, 2, 9, 0, 0, tzinfo=UTC)


@pytest.mark.requirement("OPEN-01")
def test_wrap_minimal() -> None:
    out = wrap(128.0, source="rekordbox", modified_at=_FIXED)
    assert out["value"] == 128.0
    assert out["source"] == "rekordbox"
    assert out["modified_at"].endswith("Z")


@pytest.mark.requirement("OPEN-01")
def test_wrap_requires_modified_at_explicitly() -> None:
    """No hidden wall-clock default: omitting modified_at is a TypeError,
    not a silent now(). Regression for the PR #4119 CI flake where two
    exports of the same untouched library disagreed on bytes because
    ``wrap()`` quietly stamped ``datetime.now()`` per call."""
    with pytest.raises(TypeError):
        wrap(128.0, source="rekordbox")  # type: ignore[call-arg]


@pytest.mark.requirement("OPEN-01")
def test_wrap_explicit_datetime() -> None:
    dt = datetime(2026, 2, 2, 9, 0, 0, tzinfo=UTC)
    out = wrap(128.0, source="rekordbox", modified_at=dt)
    assert out["modified_at"] == "2026-02-02T09:00:00Z"


@pytest.mark.requirement("OPEN-01")
def test_wrap_naive_datetime_assumed_utc() -> None:
    dt = datetime(2026, 2, 2, 9, 0, 0)  # no tzinfo
    out = wrap(128.0, source="rekordbox", modified_at=dt)
    assert out["modified_at"] == "2026-02-02T09:00:00Z"


@pytest.mark.requirement("OPEN-01")
def test_wrap_string_timestamp_passthrough() -> None:
    out = wrap(128.0, source="rekordbox", modified_at="2026-01-01T12:00:00Z")
    assert out["modified_at"] == "2026-01-01T12:00:00Z"


@pytest.mark.requirement("OPEN-01")
def test_wrap_confidence_included_when_set() -> None:
    out = wrap(128.0, source="rekordbox", modified_at=_FIXED, confidence=0.85)
    assert out["confidence"] == 0.85


@pytest.mark.requirement("OPEN-01")
def test_wrap_confidence_omitted_when_none() -> None:
    out = wrap(128.0, source="rekordbox", modified_at=_FIXED)
    assert "confidence" not in out


@pytest.mark.requirement("OPEN-01")
def test_wrap_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="not a valid"):
        wrap(128.0, source="not-a-vendor", modified_at=_FIXED)


@pytest.mark.requirement("OPEN-01")
def test_wrap_accepts_all_enum_sources() -> None:
    for src in ["mik", "rekordbox", "djay", "serato", "traktor", "open-dj-tool", "manual", "inferred"]:
        wrap(1, source=src, modified_at=_FIXED)


@pytest.mark.requirement("OPEN-02")
def test_utc_mtime_is_aware_utc_and_wraps_to_z(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    path.write_bytes(b"x")
    pin = datetime(2024, 3, 1, 8, 0, 0, tzinfo=UTC)
    os.utime(path, (pin.timestamp(), pin.timestamp()))
    stamp = utc_mtime(path)
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0
    out = wrap(128.0, source="djay", modified_at=stamp)
    assert out["modified_at"] == "2024-03-01T08:00:00Z"
