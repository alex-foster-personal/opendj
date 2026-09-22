"""Unit tests for reviewer lease enforcement (issue #272)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.review_gh import TriageError
from scripts.review_lease import (
    LeaseBlocked,
    ReviewerLease,
    active_reviewer,
    assert_update_allowed,
    format_blocked,
    lease_from_pr_view,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "review_lease"
_HEAD = "abcdef0123456789abcdef0123456789abcdef01"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_active_reviewer_returns_codex_from_fixture() -> None:
    data = _load("pr_open_codex.json")
    assert active_reviewer([label["name"] for label in data["labels"]]) == "codex"


def test_active_reviewer_returns_none_after_handoff() -> None:
    data = _load("pr_handoff.json")
    assert active_reviewer([label["name"] for label in data["labels"]]) is None


def test_active_reviewer_errors_on_ambiguous_fixture() -> None:
    data = _load("pr_ambiguous.json")
    with pytest.raises(TriageError, match="ambiguous reviewer lease"):
        active_reviewer([label["name"] for label in data["labels"]])


def test_assert_update_allowed_blocks_non_owner() -> None:
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    with pytest.raises(LeaseBlocked):
        assert_update_allowed(lease, None)


def test_assert_update_allowed_allows_owner() -> None:
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    assert_update_allowed(lease, "codex")


def test_format_blocked_names_reviewer_and_head() -> None:
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    msg = format_blocked(lease)
    assert "reviewer:codex" in msg
    assert _HEAD in msg
    assert "MDT_REVIEWER_FLEET=codex" in msg


def test_lease_from_pr_view_builds_lease_from_fixture() -> None:
    data = _load("pr_open_codex.json")
    lease = lease_from_pr_view(str(data["number"]), data["labels"], data["headRefOid"])
    assert lease is not None
    assert lease.fleet == "codex"
    assert lease.head_sha == data["headRefOid"]
