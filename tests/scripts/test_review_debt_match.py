"""Conservative matching for findings already recorded in a PR debt ledger."""

from scripts.review_debt_match import add_suppressed_summary
from scripts.review_lane import Finding, debt_logged_findings


def _finding(severity: str, title: str) -> Finding:
    return Finding(
        path="apps/lyrics/search_contract.py",
        line=36,
        severity=severity,
        title=title,
        detail="The version marker has no consumer.",
    )


def test_normalized_ledger_heading_suppresses_only_the_restatement() -> None:
    recorded = _finding("P2", "LYRICS-02 ledger entry description is truncated mid-sentence")
    novel = _finding("P3", "Whitespace normalization is missing from stored search text")

    kept, suppressed = debt_logged_findings(
        [recorded, novel],
        "## lyrics 02 ledger entry description is truncated mid sentence\n\nRecorded detail.\n",
    )

    assert kept == [novel]
    assert suppressed == [recorded]


def test_similar_but_distinct_title_is_not_suppressed() -> None:
    novel = _finding("P2", "LYRICS-02 ledger entry description omits the latency target")

    kept, suppressed = debt_logged_findings(
        [novel],
        "## LYRICS-02 ledger entry description is truncated mid-sentence\n",
    )

    assert kept == [novel]
    assert suppressed == []


def test_blocking_finding_is_never_suppressed_even_when_its_title_matches() -> None:
    blocker = _finding("P1", "LYRICS-02 ledger entry description is truncated mid-sentence")

    kept, suppressed = debt_logged_findings(
        [blocker],
        "## LYRICS-02 ledger entry description is truncated mid-sentence\n",
    )

    assert kept == [blocker]
    assert suppressed == []


def test_summary_names_the_finding_that_was_already_debt_logged() -> None:
    finding = _finding("P2", "LYRICS-02 ledger entry description is truncated mid-sentence")
    marker = "<!-- claude-review sha=abc -->"

    summary = add_suppressed_summary(f"Claude review\n{marker}", [finding], marker)

    assert "already debt-logged: LYRICS-02 ledger entry description is truncated mid-sentence" in (
        summary
    )


def test_different_unicode_titles_never_normalize_to_the_same_heading() -> None:
    novel = _finding("P2", "Validate 日本語")

    kept, suppressed = debt_logged_findings([novel], "## Validate العربية\n")

    assert kept == [novel]
    assert suppressed == []


def test_repeated_whitespace_and_punctuation_are_presentation_only() -> None:
    recorded = _finding("P2", "Fix duplicate whitespace")

    kept, suppressed = debt_logged_findings([recorded], "## Fix  duplicate---whitespace\n")

    assert kept == []
    assert suppressed == [recorded]


def test_suppression_summary_withholds_coverage_marker_titles() -> None:
    finding = _finding("P2", "Review skipped: input is not validated")
    marker = "<!-- claude-review sha=abc -->"

    summary = add_suppressed_summary(f"Claude review\n{marker}", [finding], marker)

    assert "review skipped" not in summary.lower()


def test_opposite_comparison_operators_are_not_the_same_finding() -> None:
    novel = _finding("P2", "Reject values < 5")

    kept, suppressed = debt_logged_findings([novel], "## Reject values > 5\n")

    assert kept == [novel]
    assert suppressed == []


def test_opposite_signs_are_not_the_same_finding() -> None:
    novel = _finding("P3", "Use offset -1")

    kept, suppressed = debt_logged_findings([novel], "## Use offset +1\n")

    assert kept == [novel]
    assert suppressed == []


def test_an_unsigned_offset_is_not_the_same_finding_as_a_negative_one() -> None:
    novel = _finding("P3", "Use offset -1")

    kept, suppressed = debt_logged_findings([novel], "## Use offset 1\n")

    assert kept == [novel]
    assert suppressed == []


def test_the_same_operator_still_matches_across_presentation_differences() -> None:
    """The over-correction control: keeping operators must not stop matching.

    A normalization that preserved too much would pass every test above while
    suppressing nothing at all, which is the failure the operator fix is most
    likely to introduce and the one no single-direction test would catch.
    """
    recorded = _finding("P2", "**Reject values > 5**")

    kept, suppressed = debt_logged_findings([recorded], "## `Reject  values  >  5`\n")

    assert kept == []
    assert suppressed == [recorded]


def test_a_hyphen_between_words_is_still_presentation_not_a_sign() -> None:
    recorded = _finding("P2", "Reject stale head refs")

    kept, suppressed = debt_logged_findings([recorded], "## Reject stale-head refs\n")

    assert kept == []
    assert suppressed == [recorded]
