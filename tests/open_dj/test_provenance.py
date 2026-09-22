"""Tests for :mod:`apps.open_dj.provenance`."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.open_dj.provenance import wrap


@pytest.mark.requirement("OPEN-01")
def test_wrap_minimal() -> None:
    out = wrap(128.0, source="rekordbox")
    assert out["value"] == 128.0
    assert out["source"] == "rekordbox"
    assert out["modified_at"].endswith("Z")


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
    out = wrap(128.0, source="rekordbox", confidence=0.85)
    assert out["confidence"] == 0.85


@pytest.mark.requirement("OPEN-01")
def test_wrap_confidence_omitted_when_none() -> None:
    out = wrap(128.0, source="rekordbox")
    assert "confidence" not in out


@pytest.mark.requirement("OPEN-01")
def test_wrap_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="not a valid"):
        wrap(128.0, source="not-a-vendor")


@pytest.mark.requirement("OPEN-01")
def test_wrap_accepts_all_enum_sources() -> None:
    for src in ["mik", "rekordbox", "djay", "serato", "traktor",
                "open-dj-tool", "manual", "inferred"]:
        wrap(1, source=src)
