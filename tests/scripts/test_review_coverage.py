"""Does review-triage tell a real review from a reviewer that merely said pass?

The tool exists because both bot reviewers on this repo report `pass` while
doing no review. A classifier that answered NOT REVIEWED for everything would
"catch" that and be worthless, so these tests pin BOTH directions.
"""

from __future__ import annotations

import pytest

from scripts.review_coverage import (
    EXPECTED_REVIEWERS,
    ReviewerVerdict,
    TriageError,
    classify_reviewer,
    main,
)


def _check(name: str, description: str, bucket: str = "pass") -> dict[str, str]:
    return {"name": name, "bucket": bucket, "state": "SUCCESS", "description": description}


# ----- the reviewer did NOT review ---------------------------------------


def test_absent_check_is_not_a_pass() -> None:
    verdict = classify_reviewer("Devin Review", [_check("CodeRabbit", "ok")])
    assert verdict.reviewed is False
    assert "no check" in verdict.reason


def test_rate_limited_is_not_a_review() -> None:
    checks = [_check("CodeRabbit", "Review rate limited")]
    assert classify_reviewer("CodeRabbit", checks).reviewed is False


def test_expired_trial_is_not_a_review() -> None:
    checks = [
        _check("Devin Review", "Full review skipped: trial expired and no credits remaining")
    ]
    assert classify_reviewer("Devin Review", checks).reviewed is False


def test_still_running_is_not_a_review() -> None:
    checks = [_check("CodeRabbit", "", bucket="pending")]
    assert classify_reviewer("CodeRabbit", checks).reviewed is False


def test_skipped_check_is_not_a_review() -> None:
    checks = [_check("CodeRabbit", "", bucket="skipping")]
    assert classify_reviewer("CodeRabbit", checks).reviewed is False


# ----- the reviewer DID review -------------------------------------------


def test_a_real_review_is_recognized() -> None:
    """THE CONTROL THAT MATTERS. Taken from PR #528's actual description, the
    last PR Devin really reviewed before its trial expired at #530. Without
    this, a classifier stuck at NOT REVIEWED would pass every other test here."""
    checks = [_check("Devin Review", "Completed analysis in 1m 42s")]
    verdict = classify_reviewer("Devin Review", checks)
    assert verdict.reviewed is True
    assert verdict.reason == "Completed analysis in 1m 42s"


def test_a_pass_with_no_description_is_a_review() -> None:
    checks = [_check("CodeRabbit", "")]
    assert classify_reviewer("CodeRabbit", checks).reviewed is True


def test_markers_match_case_insensitively() -> None:
    checks = [_check("CodeRabbit", "REVIEW RATE LIMITED")]
    assert classify_reviewer("CodeRabbit", checks).reviewed is False


# ----- shape and failure-to-measure --------------------------------------


def test_expected_reviewers_is_not_empty() -> None:
    """An empty reviewer list would make every triage vacuously clean, which is
    the exact defect this tool exists to catch, one level up."""
    assert len(EXPECTED_REVIEWERS) > 0


def test_verdict_is_immutable() -> None:
    verdict = ReviewerVerdict("x", True, "y")
    with pytest.raises(Exception):
        verdict.name = "z"  # type: ignore[misc]


def test_bad_usage_exits_two() -> None:
    assert main(["review_triage"]) == 2


def test_triage_error_is_distinct_from_a_verdict() -> None:
    """Exit 3 means COULD NOT MEASURE, never 'measured and found bad'. A caller
    that cannot tell those apart will read an API outage as a clean board."""
    assert issubclass(TriageError, RuntimeError)


# ----- artifacts, not status ---------------------------------------------
# Added Tue 1 Sep 2026 after the status layer alone proved insufficient live.

from scripts.review_coverage import ReviewerEvidence  # noqa: E402


def _evid(reviews: int = 0, inline: int = 0, bodies: tuple[str, ...] = ()) -> ReviewerEvidence:
    return ReviewerEvidence(reviews, inline, bodies)


def test_review_completed_with_no_artifact_is_not_a_review() -> None:
    """THE CASE THAT DEFEATED THE STATUS-ONLY VERSION. PR #682 carried
    CodeRabbit 'Review completed' with zero submitted reviews and zero inline
    comments. A status that reads like success is the most dangerous kind of
    hollow signal, because nothing about it invites a second look."""
    checks = [_check("CodeRabbit", "Review completed")]
    assert classify_reviewer("CodeRabbit", checks, _evid()).reviewed is False
    assert "NO review artifact" in classify_reviewer("CodeRabbit", checks, _evid()).reason


def test_rate_limit_in_the_body_counts_even_when_the_status_is_clean() -> None:
    """CodeRabbit reports its rate limit in the COMMENT, not the check
    description, so a status-only probe reads it as a pass."""
    checks = [_check("CodeRabbit", "Review completed")]
    bodies = ("> [!WARNING]\n> ## Review limit reached\n> rate limited by coderabbit.ai",)
    verdict = classify_reviewer("CodeRabbit", checks, _evid(bodies=bodies))
    assert verdict.reviewed is False


def test_a_real_artifact_backed_review_passes() -> None:
    """The control for the two above. Without it, an always-MISS classifier
    would satisfy both and look like a working tool."""
    checks = [_check("CodeRabbit", "Review completed")]
    bodies = ("<details><summary>Recent review info</summary>Files selected for processing (2)",)
    verdict = classify_reviewer("CodeRabbit", checks, _evid(bodies=bodies))
    assert verdict.reviewed is True


def test_inline_comments_alone_are_a_valid_artifact() -> None:
    checks = [_check("Devin Review", "Completed analysis in 1m 42s")]
    assert classify_reviewer("Devin Review", checks, _evid(inline=10)).reviewed is True


def test_artifact_count_sums_all_three_sources() -> None:
    assert _evid(reviews=1, inline=10, bodies=("x",)).artifact_count == 12
