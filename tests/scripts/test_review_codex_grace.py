"""Does the review chain give Codex its grace on a fresh head, and only then?

Ported from #4546's tests/scripts/test_review_fallback.py when the review chain
(REVIEW-15) absorbed that module (REVIEW-12). Every payload below is a RECORDED
shape, trimmed only in long boilerplate bodies: Codex's usage-limit notice on
PR #4538 and its reviews and later notice on PR #4515, with check-suite
creation times read from the same heads (Wed 30 Sep 2026). `decide` is pure,
so nothing of ours is mocked; the gh wiring is proven by live calls at the
bottom, skipped only when MDT_LIVE_GITHUB is not set.

Each rule is pinned in both directions, per .claude/rules/verification.md: a
case that must fire and a case that must not, so neither an always-proceed nor
an always-wait gate can pass the file.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from scripts.review_codex_grace import (
    CFG,
    CodexGateError,
    codex_artifacts,
    codex_artifacts_on,
    decide,
    pushed_at,
    pushed_at_from_check_suites,
)
from scripts.review_gh import TriageError

CODEX = {"login": "chatgpt-codex-connector[bot]"}

# ----- recorded shapes -----------------------------------------------------

#: PR #4538, head 30392faa07, issue comment id 5919853769, verbatim body.
NOTICE_4538 = {
    "user": CODEX,
    "created_at": "2026-09-30T21:14:22Z",
    "body": (
        "You have reached your Codex usage limits for code reviews. You can see your "
        "limits in the [Codex usage dashboard](https://chatgpt.com/codex/cloud/settings/usage)."
        "\nTo continue using code reviews, add credits to your account and enable them for "
        "code reviews in your [settings](https://chatgpt.com/codex/cloud/settings/code-review)."
    ),
}
HEAD_4538 = "30392faa07bc095990539ff80a27b6e4cd155803"
#: Earliest of the 26 check suites GitHub created for HEAD_4538. The commit's
#: committer date, 21:13:33Z, is 43 s EARLIER and is deliberately not used.
SUITES_4538 = [
    {
        "total_count": 3,
        "check_suites": [
            {"app": {"slug": "sourcery-ai"}, "created_at": "2026-09-30T21:14:16Z"},
            {"app": {"slug": "github-actions"}, "created_at": "2026-09-30T21:14:21Z"},
            {"app": {"slug": "trunk-io"}, "created_at": "2026-09-30T21:14:17Z"},
        ],
    },
]

#: PR #4515: a Codex review of an earlier head (body trimmed).
REVIEW_4515_EE20 = {
    "user": CODEX,
    "submitted_at": "2026-09-30T00:37:37Z",
    "commit_id": "ee20d0cc0c1489ee45b0440813188424251e539c",
    "state": "COMMENTED",
    "body": "\n### Codex Review\n\n**Reviewed commit:** `ee20d0cc0c`\n",
}
#: The notice Codex left at 07:51:38Z, answering head d7812c36c8. The PR's
#: final head ff2936d255 was pushed at 14:17:23Z, SIX HOURS LATER.
OLD_NOTICE_4515 = {**NOTICE_4538, "created_at": "2026-09-30T07:51:38Z"}
HEAD_4515_FINAL = "ff2936d255e6870075bff60d4aa04cfacab4a5b0"
PUSH_4515_FINAL = datetime(2026, 9, 30, 14, 17, 23, tzinfo=UTC)
HEAD_4515_8FC0 = "8fc0eb2cd2af2b27b030d6920e2afffc1aba5ff1"
PUSH_4515_8FC0 = datetime(2026, 9, 30, 1, 27, 23, tzinfo=UTC)
PUSH_4538 = pushed_at_from_check_suites(SUITES_4538)


def _after(start: datetime, minutes: float) -> datetime:
    return start + timedelta(minutes=minutes)


def _decide(head: str, pushed: datetime, minutes: float, reviews=(), comments=()):
    artifacts = codex_artifacts(list(reviews), list(comments))
    return decide(head, pushed, artifacts, _after(pushed, minutes))


# ----- the notice ----------------------------------------------------------


@pytest.mark.requirement("REVIEW-12")
def test_a_notice_six_seconds_after_the_push_proceeds_at_once() -> None:
    """[if] a usage-limit notice follows the push [then] the chain proceeds, [else stop].

    #4538 as it happened. Ignoring the notice would hold the head for 20
    minutes waiting on a reviewer that already said no."""
    gate = _decide(HEAD_4538, PUSH_4538, 1, comments=[NOTICE_4538])
    assert gate.proceed
    assert "usage-limit notice at Wed 30 Sep 21:14Z" in gate.reason


@pytest.mark.requirement("REVIEW-12")
def test_a_notice_left_for_an_earlier_head_does_not_skip_the_grace() -> None:
    """[if] the only notice predates the push [then] the chain waits, [else stop].

    #4515's 07:51Z notice answered an older head. Read as current, it would
    skip Codex's grace on every push that follows a walled one."""
    gate = _decide(HEAD_4515_FINAL, PUSH_4515_FINAL, 5, comments=[OLD_NOTICE_4515])
    assert not gate.proceed
    assert "15 min of grace left" in gate.reason


def test_an_old_notice_still_yields_to_silence_once_the_grace_runs_out() -> None:
    gate = _decide(HEAD_4515_FINAL, PUSH_4515_FINAL, 25, comments=[OLD_NOTICE_4515])
    assert gate.proceed
    assert "silent 25 min" in gate.reason


def test_a_notice_submitted_as_a_review_at_the_head_proceeds() -> None:
    """NOT a recorded shape: Codex has only been seen posting the notice as an
    issue comment. Pinned anyway, tied to the head by SHA rather than date."""
    as_review = {**REVIEW_4515_EE20, "commit_id": HEAD_4515_8FC0, "body": NOTICE_4538["body"]}
    assert _decide(HEAD_4515_8FC0, PUSH_4515_8FC0, 1, reviews=[as_review]).proceed


def test_the_push_time_is_the_earliest_check_suite_not_the_commit_date() -> None:
    assert datetime(2026, 9, 30, 21, 14, 16, tzinfo=UTC) == PUSH_4538


def test_a_head_with_no_check_suite_is_unmeasured_not_undated() -> None:
    with pytest.raises(CodexGateError, match="push time is unknown"):
        pushed_at_from_check_suites([{"total_count": 0, "check_suites": []}])


def test_an_impostor_login_cannot_post_the_notice() -> None:
    impostor = {**NOTICE_4538, "user": {"login": "chatgpt-codex-connector-attacker"}}
    assert not _decide(HEAD_4538, PUSH_4538, 1, comments=[impostor]).proceed


def test_codex_artifacts_keeps_only_codex_and_reads_both_kinds() -> None:
    human = {"user": {"login": "someone"}, "created_at": "2026-09-30T01:00:00Z", "body": "x"}
    found = codex_artifacts([REVIEW_4515_EE20], [human, OLD_NOTICE_4515])
    assert [a.kind for a in found] == ["review", "comment"]
    assert found[0].commit_id == REVIEW_4515_EE20["commit_id"]
    assert found[1].at == datetime(2026, 9, 30, 7, 51, 38, tzinfo=UTC)


# ----- silence -------------------------------------------------------------


@pytest.mark.requirement("REVIEW-12")
@pytest.mark.parametrize(
    ("minutes", "proceed"), [(19, False), (19.99, False), (20, True), (21, True)]
)
def test_silence_waits_inside_the_grace_and_proceeds_past_it(minutes: float, proceed: bool) -> None:
    """[if] Codex is silent [then] the chain waits 20 min and then proceeds, [else stop]."""
    assert _decide(HEAD_4515_FINAL, PUSH_4515_FINAL, minutes).proceed is proceed


def test_a_codex_review_of_an_earlier_head_does_not_cut_the_grace_short() -> None:
    assert not _decide(HEAD_4515_8FC0, PUSH_4515_8FC0, 2, reviews=[REVIEW_4515_EE20]).proceed
    assert _decide(HEAD_4515_8FC0, PUSH_4515_8FC0, 30, reviews=[REVIEW_4515_EE20]).proceed


def test_the_grace_is_twenty_minutes() -> None:
    assert timedelta(minutes=20) == CFG.CODEX_GRACE


# ----- bad inputs are errors -----------------------------------------------


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(CodexGateError, match="timezone-aware"):
        decide(HEAD_4538, PUSH_4538, (), datetime(2026, 9, 30, 21, 20))  # noqa: DTZ001 - naive on purpose


def test_a_push_after_now_is_clock_skew_not_a_wait() -> None:
    with pytest.raises(CodexGateError, match="clock skew"):
        decide(HEAD_4538, PUSH_4538, (), PUSH_4538 - timedelta(seconds=1))


def test_a_gate_error_is_a_triage_error_so_the_chain_reads_it_as_unmeasured() -> None:
    assert issubclass(CodexGateError, TriageError)


# ----- live gh wiring: no stand-in for gh ----------------------------------


MDT_LIVE_GITHUB = os.environ.get("MDT_LIVE_GITHUB") == "1"
LIVE_GITHUB_UNAVAILABLE = (
    "UNAVAILABLE: live GitHub API checks not run. This is a capability report, not a "
    "pass. Enable with MDT_LIVE_GITHUB=1 and gh authenticated (gh auth status)."
)


def live(fn):
    fn = pytest.mark.skipif(not MDT_LIVE_GITHUB, reason=LIVE_GITHUB_UNAVAILABLE)(fn)
    return pytest.mark.live_github(fn)


@pytest.fixture
def gh_authenticated() -> None:
    """Opting in is a claim that gh works, so a failure here fails, never skips."""
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"MDT_LIVE_GITHUB=1 but gh is not authenticated: {result.stderr.strip()}")


@live
@pytest.mark.requirement("REVIEW-12")
def test_a_gh_error_is_raised_never_read_as_no_notice(gh_authenticated: None) -> None:
    """[if] a gh read fails [then] it raises rather than reading as silence, [else stop].

    Negative control: a PR that does not exist must raise, not come back
    as zero artifacts, which `decide` would read as silence."""
    with pytest.raises(TriageError):
        codex_artifacts_on("999999999")


@live
def test_the_real_4515_payloads_parse_to_the_recorded_shapes(gh_authenticated: None) -> None:
    """Positive control for the negative one above: the live read finds the
    recorded review and the recorded notice."""
    found = codex_artifacts_on("4515")
    assert any(a.kind == "review" and a.commit_id == REVIEW_4515_EE20["commit_id"] for a in found)
    assert any(a.at == datetime(2026, 9, 30, 7, 51, 38, tzinfo=UTC) for a in found)


@live
def test_the_real_push_time_of_4515s_final_head(gh_authenticated: None) -> None:
    assert pushed_at(HEAD_4515_FINAL) == PUSH_4515_FINAL
