"""Does review-triage tell a real review from a reviewer that merely said pass?

The tool exists because both bot reviewers on this repo report `pass` while
doing no review. A classifier that answered NOT REVIEWED for everything would
"catch" that and be worthless, so these tests pin BOTH directions.
"""

from __future__ import annotations

import pytest

from scripts.review_coverage import (
    CHECKLESS_REVIEWERS,
    EXPECTED_REVIEWERS,
    REVIEWER_LOGINS,
    ReviewerEvidence,
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


# ----- known-unavailable reviewers ---------------------------------------
# A gate that can never go green blocks all work. A silent exemption is how
# "never reviewed" starts reading as "clean" again. These pin both edges.

from scripts.review_coverage import KNOWN_UNAVAILABLE_REVIEWERS  # noqa: E402


def test_the_exemption_list_is_not_a_blanket() -> None:
    """If every expected reviewer were exempt the gate could never fail, which
    is the exact defect this whole module exists to catch, one level up."""
    assert set(KNOWN_UNAVAILABLE_REVIEWERS) != set(EXPECTED_REVIEWERS)
    assert len(KNOWN_UNAVAILABLE_REVIEWERS) < len(EXPECTED_REVIEWERS)


def test_every_exemption_states_a_reason() -> None:
    """An exemption with no reason and no owner is the note that outlives the
    outage it describes."""
    for name, reason in KNOWN_UNAVAILABLE_REVIEWERS.items():
        assert len(reason) > 20, f"{name} exemption needs a real reason"


def test_exempt_reviewers_are_reviewers_we_actually_expect() -> None:
    """Guards a typo silently exempting nothing, which would read as a working
    exemption while the gate stayed permanently red."""
    for name in KNOWN_UNAVAILABLE_REVIEWERS:
        assert name in EXPECTED_REVIEWERS, f"{name} is not in EXPECTED_REVIEWERS"


# ----- an exemption must not swallow a reviewer that is STILL RUNNING ------

from scripts.review_coverage import partition_verdicts  # noqa: E402


def _verdicts(
    checks: list[dict[str, str]],
    names: tuple[str, ...] = ("CodeRabbit", "Devin Review"),
) -> list[ReviewerVerdict]:
    """Real verdicts, produced by the production classifier from real check
    payload shapes. No stand-in stands in front of `classify_reviewer`.

    `names` defaults to two representative CHECK-BASED reviewer names, not to
    the live `EXPECTED_REVIEWERS` (Codex-only and checkless since issue #1016,
    Thu 3 Sep 2026). The exemption mechanism this section pins is generic over
    any reviewer that posts a check, and exercising it needs a name that CAN
    reach `pending` and outage states, which a checkless entry never can (see
    CHECKLESS_REVIEWERS in scripts/review_coverage.py). The default parameter
    binding under test -- `partition_verdicts` reading production
    `KNOWN_UNAVAILABLE_REVIEWERS` when no policy is passed -- does not depend
    on which names are currently expected either.
    """
    return [classify_reviewer(name, checks) for name in names]


def test_an_exempt_reviewer_that_is_still_running_still_blocks() -> None:
    """Devin is exempt because it is DOWN, not because its output is optional.

    The day it is re-enabled its check goes `pending` before it goes anywhere
    else. If the exemption covered that, this gate would pass the moment the
    other reviewers finished, and the PR could merge while Devin was still
    writing findings that would then never get a pre-merge disposition.
    """
    verdicts = _verdicts([
        _check("CodeRabbit", "Review completed"),
        _check("Devin Review", "Running", bucket="pending"),
    ])
    devin = next(v for v in verdicts if v.name == "Devin Review")
    assert devin.in_progress is True

    unavailable, unreviewed, _ = partition_verdicts(verdicts)
    assert [v.name for v in unavailable] == []
    assert "Devin Review" in [v.name for v in unreviewed]


def test_an_exempt_reviewer_that_reports_its_outage_does_not_block() -> None:
    """The control for the test above, and the reason the exemption exists.

    Without this pair, restricting the exemption to nothing at all would
    satisfy the blocking test and leave the gate permanently red, which is the
    failure this change was made to fix.

    The payload is Devin's ACTUAL live description, verbatim from
    `gh pr checks` on Tue 1 Sep 2026. An earlier version of this test used a
    PR with no Devin row at all and asserted that absence was exempt. That was
    wrong twice over: it is not what the live check emits, and it made the
    exemption rest on an absence, which is what let a `skipping` bucket and a
    stale comment body both pass for an outage. Absence is now handled by the
    test below, and it blocks.
    """
    verdicts = _verdicts([
        _check("CodeRabbit", "Review completed"),
        _check(
            "Devin Review",
            "Full review skipped: trial expired and no credits remaining",
        ),
    ])
    devin = next(v for v in verdicts if v.name == "Devin Review")
    assert devin.in_progress is False
    assert devin.outage is True, "the live outage description must certify the outage"

    # The mapping is an ARGUMENT here, so this pins the partition MECHANISM
    # independently of whether any reviewer happens to be exempt today. That
    # matters because the live list is empty as of Tue 1 Sep 2026 (Devin came
    # back), and a mechanism only exercised while an outage exists is untested
    # at exactly the moment the next outage needs it to work.
    #
    # What this test does NOT cover is the default wiring, which is the gap
    # Codex named on #704 (discussion_r3903503396). That is covered directly by
    # test_the_live_policy_exempts_nobody_and_that_is_load_bearing below, which
    # calls partition_verdicts with NO argument.
    unavailable, unreviewed, _ = partition_verdicts(
        verdicts, known_unavailable={"Devin Review": "down"}
    )
    assert [v.name for v in unavailable] == ["Devin Review"]
    assert [v.name for v in unreviewed] == []


def test_the_live_policy_exempts_nobody_and_that_is_load_bearing() -> None:
    """The DEFAULT wiring, exercised with no injected policy at all.

    `partition_verdicts` gained a `known_unavailable` parameter so the exemption
    mechanism stays pinned while the production list is empty. That parameter
    also created a hole: every other test passes its own mapping, so nothing
    would notice if the default stopped reading the production one. A test suite
    that is green because it never touches the live wiring is the same defect
    this module exists to catch, one level up.

    So this calls `partition_verdicts` exactly as `triage()` does, with real
    captured check descriptions and no policy argument, and asserts the
    consequence of today's empty list: a reviewer whose outage is certified is
    still NOT exempt, and blocks. It fails if the default is rewired to
    something else, and it fails if an exemption is re-added without anyone
    updating the tests that describe the policy.
    """
    verdicts = _verdicts([
        _check("CodeRabbit", "Review completed"),
        _check(
            "Devin Review",
            "Full review skipped: trial expired and no credits remaining",
        ),
    ])
    devin = next(v for v in verdicts if v.name == "Devin Review")
    assert devin.outage is True, (
        "the outage is certified, so exemption is the ONLY thing that could "
        "excuse this reviewer; without that this test would prove nothing"
    )

    unavailable, unreviewed, revived = partition_verdicts(verdicts)

    assert [v.name for v in unavailable] == [], (
        "the live exemption list is empty, so nothing may be exempt; an entry "
        "was re-added without updating the tests that state the policy"
    )
    assert [v.name for v in unreviewed] == ["Devin Review"]
    assert [v.name for v in revived] == []


def test_an_exempt_reviewer_that_reports_nothing_at_all_blocks() -> None:
    """No check row is not evidence of an outage; it is evidence of nothing.

    A reviewer that reports nothing may be out of credit, uninstalled, or
    misconfigured, and those need different responses. Blocking is the honest
    verdict, and it is also the one that forces `EXPECTED_REVIEWERS` to be
    corrected rather than quietly carrying a name nothing answers to.
    """
    verdicts = _verdicts([_check("CodeRabbit", "Review completed")])
    devin = next(v for v in verdicts if v.name == "Devin Review")
    assert devin.outage is False

    unavailable, unreviewed, _ = partition_verdicts(verdicts)
    assert [v.name for v in unavailable] == []
    assert "Devin Review" in [v.name for v in unreviewed]


def test_a_reviewer_with_no_exemption_blocks_whether_pending_or_absent() -> None:
    """Guards the guard: `in_progress` must not become a second exemption.

    A non-exempt reviewer that is still running is still not a review, so it
    belongs in `unreviewed` exactly as an absent one does.
    """
    for bucket, description in (("pending", "Running"), ("fail", "Review rate limited")):
        verdicts = _verdicts([_check("CodeRabbit", description, bucket=bucket)])
        _, unreviewed, _ = partition_verdicts(verdicts)
        assert "CodeRabbit" in [v.name for v in unreviewed], bucket


def test_a_pending_rerun_is_not_exempted_by_its_own_stale_outage_comment() -> None:
    """Two instruments disagree, and the ORDER decided which one was heard.

    Devin's outage comment stays on the PR forever. When Devin is re-enabled and
    rerun, that historical body is still sitting there, so a marker scan placed
    ahead of the bucket check matched it, returned `in_progress=False`, and
    `partition_verdicts` exempted a reviewer that was at that moment writing
    findings. A PAST run outvoted the CURRENT one, which is the pending-swallow
    defect wearing a different hat: the guard existed, and a stale artifact made
    it unreachable.
    """
    checks = [
        _check("CodeRabbit", "Review completed"),
        _check("Devin Review", "Running", bucket="pending"),
    ]
    # The artifact Devin leaves when its trial lapses. Still on the PR after the
    # account is topped up and the rerun has started.
    stale = _evid(bodies=("Devin's trial expired. Add credits to continue.",))
    verdicts = [
        classify_reviewer("CodeRabbit", checks, _evid(reviews=1)),
        classify_reviewer("Devin Review", checks, stale),
    ]

    devin = next(v for v in verdicts if v.name == "Devin Review")
    assert devin.in_progress is True, "a live rerun was read as a historical outage"

    unavailable, unreviewed, _ = partition_verdicts(verdicts)
    assert [v.name for v in unavailable] == []
    assert "Devin Review" in [v.name for v in unreviewed]


def test_a_skipped_or_canceled_run_is_not_an_outage_and_still_blocks() -> None:
    """`skipping` and `cancel` are terminal non-reviews with nothing to do with
    credits, so the credit exemption must not swallow them.

    The exemption used to key on "this reviewer is on the known-down list and
    did not review", which is an ABSENCE. Every way of failing to review then
    looked identical to being out of credit.
    """
    for bucket in ("skipping", "cancel"):
        verdicts = _verdicts([
            _check("CodeRabbit", "Review completed"),
            _check("Devin Review", "", bucket=bucket),
        ])
        devin = next(v for v in verdicts if v.name == "Devin Review")
        assert devin.outage is False, bucket

        unavailable, unreviewed, _ = partition_verdicts(verdicts)
        assert [v.name for v in unavailable] == [], bucket
        assert "Devin Review" in [v.name for v in unreviewed], bucket


def test_a_restored_reviewer_is_not_exempted_by_its_own_historical_outage_comment() -> None:
    """An outage comment never leaves the PR, so an exemption keyed on bodies
    is permanent and can never be retracted.

    Devin restored: its check reports a completed review and it left an
    artifact, but the old "trial expired" comment is still sitting on the PR.
    Scanning bodies for exemption evidence made that reviewer exempt FOREVER,
    on this PR and on every later one, so the gate would pass without anyone
    being forced to remove a list entry describing an outage that had ended.

    What this asserts is the fail-closed half: the gate BLOCKS instead of
    silently passing. It deliberately does not assert `revived`, which still
    will not fire here, because certifying the review would mean letting a
    clean description outrank a body marker and that would regress the
    reviewer-reports-success-while-doing-nothing case this tool exists for.
    A red gate is the signal that forces the exemption to be revisited.
    """
    checks = [
        _check("CodeRabbit", "Review completed"),
        _check("Devin Review", "Review completed"),
    ]
    verdicts = [
        classify_reviewer("CodeRabbit", checks, _evid(reviews=1)),
        classify_reviewer(
            "Devin Review",
            checks,
            _evid(reviews=1, bodies=("Devin's trial expired. Add credits to continue.",)),
        ),
    ]
    devin = next(v for v in verdicts if v.name == "Devin Review")
    assert devin.outage is False, "a historical body certified a current outage"

    unavailable, unreviewed, _ = partition_verdicts(verdicts)
    assert [v.name for v in unavailable] == []
    assert "Devin Review" in [v.name for v in unreviewed]


# ----- reviewer coverage policy, issue #1016 (Thu 3 Sep 2026) -------------
# Codex in; CodeRabbit and Devin out of EXPECTED_REVIEWERS. CodeRabbit is
# rate-limited on every PR and Devin's trial expired at #530, so neither
# produces a real review to require; CodeRabbit's auto-review is disabled via
# the committed .coderabbit.yaml instead of the app dashboard.


def test_codex_sol_grok_cursor_and_claude_are_the_expected_reviewers() -> None:
    """Sol joined Fri 5 Sep 2026 (issue #1211), Claude Sun 6 Sep 2026, and Grok
    and Cursor Thu 1 Oct 2026 (the reviewer failover chain), each as an
    ALTERNATIVE to the others rather than as a further requirement: see
    `review_sol.substitute_alternatives` and this suite's companions
    tests/scripts/test_review_sol.py, tests/scripts/test_review_claude.py and
    tests/scripts/test_review_subscription.py, which pin that any one alone
    covers the set and that none reviewing still fails. The ORDER is the
    chain's order, Claude last (scripts/review_chain.py)."""
    assert EXPECTED_REVIEWERS == ("Codex", "Sol", "Grok", "Cursor", "Claude")
    assert "CodeRabbit" not in EXPECTED_REVIEWERS
    assert "Devin Review" not in EXPECTED_REVIEWERS


def test_codex_alias_maps_to_its_real_bot_login() -> None:
    """`chatgpt-codex-connector` is the login `_matches` needs, verified live
    (#1049, #1051, Thu 3 Sep 2026): `pulls/<n>/reviews` carries a real
    `COMMENTED` review from `chatgpt-codex-connector[bot]`."""
    assert REVIEWER_LOGINS["Codex"] == ("chatgpt-codex-connector",)


# ----- Codex is checkless: evidence is the only instrument ----------------
# Confirmed live (#1049, #1051, Thu 3 Sep 2026): `gh pr checks` lists no
# "Codex" row at all, even on a PR carrying a real Codex review. A reviewer
# that never posts a check-run cannot be classified by the check-based path,
# which reads a permanently absent check as "no check reported" -- exactly
# indistinguishable from a reviewer that never ran.


def test_codex_is_registered_checkless() -> None:
    assert "Codex" in CHECKLESS_REVIEWERS


def test_codex_present_is_recognized_from_evidence_alone() -> None:
    """THE CONTROL THAT MATTERS for the checkless path: an empty checks list
    (Codex's permanent live shape) must not by itself mean NOT REVIEWED. Body
    text taken from a real captured Codex finding, tests/fixtures/review_threads/
    pr-576.json ('BLOCKING Validate a terminal disposition before clearing
    the gate')."""
    body = (
        "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
        "</sub></sub>  BLOCKING Validate a terminal disposition before clearing "
        "the gate**\n\nWhen a thread is merely resolved without a reply..."
    )
    verdict = classify_reviewer("Codex", [], _evid(reviews=1, bodies=(body,)))
    assert verdict.reviewed is True
    assert "2 artifact" in verdict.reason


def test_codex_absent_is_not_a_pass() -> None:
    """The other half of the control: zero artifacts on the checkless path is
    still NOT REVIEWED, exactly as a status-based MISS is for CodeRabbit/Devin.
    An always-True checkless classifier would satisfy the test above and be
    worthless; this is what rules that out."""
    verdict = classify_reviewer("Codex", [], _evid())
    assert verdict.reviewed is False
    assert "no status check to fall back on" in verdict.reason


def test_codex_ignores_any_check_payload_since_it_never_posts_one() -> None:
    """A checkless reviewer takes the evidence path even when `checks` is
    non-empty (e.g. carrying an unrelated CI job named "Codex" by coincidence
    is not something this repo has, but the branch must not depend on
    `checks` being empty to behave correctly)."""
    checks = [_check("Codex", "some unrelated status", bucket="pending")]
    verdict = classify_reviewer("Codex", checks, _evid(reviews=1, bodies=("finding",)))
    assert verdict.reviewed is True
    assert verdict.in_progress is False, "the check-based pending bucket must not be consulted"


def test_codex_inline_comments_alone_are_valid_evidence() -> None:
    verdict = classify_reviewer("Codex", [], _evid(inline=3))
    assert verdict.reviewed is True


def test_codex_a_reported_skip_in_the_body_still_fails() -> None:
    """Codex has never been observed to self-report a skip, but the checkless
    path scans bodies for the same NOT_REVIEWED_MARKERS vocabulary the
    check-based path watches for, so an artifact is not automatically a pass
    if its own text says the reviewer did not really look."""
    verdict = classify_reviewer("Codex", [], _evid(reviews=1, bodies=("review skipped: no diff",)))
    assert verdict.reviewed is False


def test_checkless_path_ignored_for_a_non_checkless_name() -> None:
    """Guards the guard: a name NOT in CHECKLESS_REVIEWERS must still take the
    check-based path even when it is handed evidence, so CodeRabbit and Devin
    (still parsed by review_thread_parse.py for triage, just no longer
    EXPECTED) keep their existing pre-#1016 behavior verbatim."""
    assert "CodeRabbit" not in CHECKLESS_REVIEWERS
    assert "Devin Review" not in CHECKLESS_REVIEWERS
    verdict = classify_reviewer("CodeRabbit", [], _evid(reviews=1, bodies=("x",)))
    assert verdict.reviewed is False
    assert "no check" in verdict.reason
