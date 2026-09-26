"""`triage()` waives reviewer coverage for a docs-only PR, and only that.

Same monkeypatch seam `test_review_coverage_carry_stdout.py` uses on the same
function: the network boundary (`_head_sha`, `_checks`, `_paginated_json_list`,
`require_gate_current_with_main`) is replaced with literal fixture data, and
`triage()` itself runs unmodified end to end, including the real
`is_docs_only` classifier. Everything downstream of that boundary (thread
triage, debt checks, gate freshness) is untouched by this exemption -- only
the reviewer-coverage requirement inside `review_coverage.triage` is waived,
so these tests exercise exactly that seam and nothing wider.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from scripts import review_coverage, review_coverage_carry
from scripts.review_gh import TriageError

_PR = "3290"
_HEAD = "a" * 40
_GATE = "0" * 40


def _wire(
    monkeypatch: pytest.MonkeyPatch, files: list[dict], *, checks_and_reviews_forbidden: bool
) -> None:
    monkeypatch.setattr(review_coverage, "require_gate_current_with_main", lambda: _GATE)
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: _HEAD)
    # The carry path reads the PR diff through review_lane.diff_of, a live gh
    # call; the same seam test_review_coverage_carry_stdout.py pins. Without it
    # the mixed and empty cases hit GitHub and fail on any host with no
    # credential (every CI pytest runner) instead of testing the exemption.
    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")

    def fake_paginated(path: str) -> list[dict]:
        if path.endswith("/files"):
            return files
        if checks_and_reviews_forbidden:
            raise AssertionError(
                f"docs-only PR must not fetch {path}: reviewer coverage is waived, "
                "so no reviewer evidence should ever be requested"
            )
        return []

    monkeypatch.setattr(review_coverage, "_paginated_json_list", fake_paginated)

    def fake_checks(pr: str) -> list[dict]:
        if checks_and_reviews_forbidden:
            raise AssertionError("docs-only PR must not fetch check statuses")
        return []

    monkeypatch.setattr(review_coverage, "_checks", fake_checks)


def _run(
    monkeypatch: pytest.MonkeyPatch, files: list[dict], *, forbid_reviewer_calls: bool
) -> tuple[int, str]:
    _wire(monkeypatch, files, checks_and_reviews_forbidden=forbid_reviewer_calls)
    buf = StringIO()
    with patch("sys.stdout", buf):
        result = review_coverage.triage(_PR)
    return result, buf.getvalue()


# ----- docs-only PR: coverage is waived, exemption stays visible ----------


@pytest.mark.requirement("REVIEW-09")
def test_docs_only_pr_passes_without_ever_fetching_reviewer_evidence() -> None:
    """[if] a PR is docs-only [then] it passes without fetching reviews, [else stop].

    The whole point: a docs-only PR must not touch `_checks` or the
    reviews/inline/issue-comments endpoints at all, not just skip failing on
    them. `_wire` raises if it does."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        result, out = _run(
            monkeypatch,
            [{"filename": "docs/x.md"}, {"filename": "README.md"}],
            forbid_reviewer_calls=True,
        )
    assert result == 0
    assert "ok docs-only: coverage not required, review is async" in out
    assert "docs/x.md" in out
    assert "README.md" in out


def test_docs_only_verdict_prints_no_reviewer_table() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        _, out = _run(monkeypatch, [{"filename": "docs/x.md"}], forbid_reviewer_calls=True)
    assert "reviewer coverage" not in out
    assert "MISS" not in out


# ----- mixed PR: coverage is still required --------------------------------


def test_mixed_pr_still_requires_coverage_and_fails_with_no_reviews() -> None:
    """A PR touching docs/x.md AND scripts/y.py is NOT docs-only, so the
    normal (failing, since no reviewer evidence exists) path runs."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        result, out = _run(
            monkeypatch,
            [{"filename": "docs/x.md"}, {"filename": "scripts/y.py"}],
            forbid_reviewer_calls=False,
        )
    assert result == 1
    assert "ok docs-only" not in out
    assert "did not review" in out


# ----- empty file list: never an exemption ---------------------------------


def test_empty_changed_file_list_does_not_exempt() -> None:
    """Zero changed files must fall through to the normal (failing) path,
    never be read as "every path (there are none) matches"."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        result, out = _run(monkeypatch, [], forbid_reviewer_calls=False)
    assert result == 1
    assert "ok docs-only" not in out


# ----- a failure to list changed files must not pass as docs-only ---------


def test_gh_failure_listing_files_is_a_measurement_failure_not_a_pass() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(review_coverage, "require_gate_current_with_main", lambda: _GATE)
        monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: _HEAD)

        def raising_paginated(path: str) -> list[dict]:
            raise TriageError(f"gh api {path} failed (1): boom")

        monkeypatch.setattr(review_coverage, "_paginated_json_list", raising_paginated)

        with pytest.raises(TriageError):
            review_coverage.triage(_PR)
