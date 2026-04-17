"""SYNC-04 regression: ``--prefer newest`` actually honours per-side
modified timestamps.

Codex P04-01: ``build_analysis_diff`` previously never plumbed the
``modified_at`` timestamps from :class:`NormalisedAnalysis` into
``resolve_conflict``, so ``prefer="newest"`` silently collapsed to
RB-default for every row.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from apps.audit.sync_diff import build_analysis_diff
from apps.shared.normalised import NormalisedAnalysis


@pytest.mark.requirement("SYNC-04")
def test_prefer_newest_uses_djay_when_djay_is_newer() -> None:
    """djay-side is newer inside the window -> accept_djay."""
    now = datetime(2026, 4, 17, 12, 0, 0)
    rb = NormalisedAnalysis(
        uuid_or_id="1",
        source="rb",
        bpm=120.0,
        modified_at=now - timedelta(days=1),
    )
    dj = NormalisedAnalysis(
        uuid_or_id="uuid-1",
        source="djay",
        bpm=124.0,
        modified_at=now,
    )

    analysis_rows, _ = build_analysis_diff(
        {"1": rb},
        {"uuid-1": dj},
        {},
        {},
        [("1", "uuid-1")],
        prefer="newest",
    )
    bpm_rows = [r for r in analysis_rows if r.field == "bpm"]
    assert len(bpm_rows) == 1
    assert bpm_rows[0].resolution == "accept_djay", (
        "prefer=newest must honour djay_modified_at when it is newer; "
        "regression of Codex P04-01 (timestamps not plumbed)."
    )


@pytest.mark.requirement("SYNC-04")
def test_prefer_newest_uses_rb_when_rb_is_newer() -> None:
    now = datetime(2026, 4, 17, 12, 0, 0)
    rb = NormalisedAnalysis(
        uuid_or_id="1",
        source="rb",
        bpm=120.0,
        modified_at=now,
    )
    dj = NormalisedAnalysis(
        uuid_or_id="uuid-1",
        source="djay",
        bpm=124.0,
        modified_at=now - timedelta(days=1),
    )

    analysis_rows, _ = build_analysis_diff(
        {"1": rb},
        {"uuid-1": dj},
        {},
        {},
        [("1", "uuid-1")],
        prefer="newest",
    )
    bpm_rows = [r for r in analysis_rows if r.field == "bpm"]
    assert len(bpm_rows) == 1
    assert bpm_rows[0].resolution == "accept_rb"
