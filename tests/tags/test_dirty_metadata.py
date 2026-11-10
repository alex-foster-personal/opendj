"""BC3 dirty-metadata candidate filter and eval seed (issue #11176)."""

from __future__ import annotations

import pytest

from apps.tags.dirty_metadata import (
    default_eval_cases_path,
    dirty_metadata_candidate,
    eval_case_decision,
    load_dirty_metadata_cases,
)
from apps.tags.title_fix import Track

_EVALS = default_eval_cases_path()


@pytest.mark.requirement("META-12014")
def test_must_not_overwrite_tag_never_becomes_candidate() -> None:
    """[if] a case row is tagged must_not_overwrite [then] it is skipped, [else stop]."""
    row = next(c for c in load_dirty_metadata_cases() if "must_not_overwrite" in c.get("tags", ()))
    decision = eval_case_decision(row)
    assert decision.candidate is False
    assert decision.skip_reason == "must_not_overwrite"


@pytest.mark.requirement("META-12014")
def test_store_clean_rows_are_skipped() -> None:
    """[if] title and artist already match store-clean form [then] skip, [else stop]."""
    for row in load_dirty_metadata_cases():
        if row["id"].startswith("clean-"):
            decision = eval_case_decision(row)
            assert not decision.candidate, row["id"]
            assert decision.skip_reason == "store_clean"


@pytest.mark.requirement("META-12014")
def test_approved_eval_rows_are_skipped() -> None:
    """[if] the row is already approved in the eval fixture [then] skip, [else stop]."""
    row = next(c for c in load_dirty_metadata_cases() if c.get("approved"))
    assert eval_case_decision(row).skip_reason == "approved"


@pytest.mark.requirement("META-12014")
def test_artistic_edge_seed_rows_are_skipped() -> None:
    """[if] an artistic edge-case title from the seed is fed [then] skip, [else stop]."""
    for row in load_dirty_metadata_cases():
        if row["id"].startswith("edge-"):
            decision = eval_case_decision(row)
            assert not decision.candidate, f"{row['id']}: {decision.skip_reason}"


@pytest.mark.requirement("META-12014")
def test_jammed_seed_rows_are_candidates() -> None:
    """[if] an obviously jammed title/artist from the seed is fed [then] candidate, [else stop]."""
    for row in load_dirty_metadata_cases():
        if row["id"].startswith("dirty-"):
            decision = eval_case_decision(row)
            assert decision.candidate, row["id"]


@pytest.mark.requirement("META-12014")
def test_eval_seed_contract() -> None:
    """[if] every dirty_metadata_cases row runs [then] expect_candidate matches, [else stop]."""
    for row in load_dirty_metadata_cases(_EVALS):
        got = eval_case_decision(row).candidate
        assert got is row["expect_candidate"], row["id"]


def test_no_llm_on_candidate_check() -> None:
    """dirty_metadata_candidate uses only local regex rules (no network)."""
    track = Track("x", "8A - 6 - Ember Tide", "")
    decision = dirty_metadata_candidate(track)
    assert decision.candidate
    assert decision.skip_reason == ""
