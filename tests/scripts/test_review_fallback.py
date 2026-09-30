"""Does the reviewer fallback run the right lane, at the right head, at the right time?

Every payload below is a RECORDED shape, trimmed only in long boilerplate
bodies: Codex's usage-limit notice on PR #4538 and its reviews, summary
comment and later notice on PR #4515, with check-suite creation times read
from the same heads (Wed 30 Sep 2026). `decide` is pure, so nothing of ours is
mocked; the gh wiring is proven by live calls at the bottom, skipped only when
gh cannot authenticate.

Each rule is pinned in both directions, per .claude/rules/verification.md: a
case that must fire and a case that must not, so neither an always-RUN_SOL nor
an always-WAIT decision can pass the file.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import claude_review, sol_review
from scripts.review_fallback import (
    CFG,
    EXIT_FOR,
    Action,
    Decision,
    FallbackError,
    HeadState,
    LaneResult,
    codex_artifacts,
    codex_artifacts_on,
    decide,
    exit_code,
    main,
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

#: PR #4515: two Codex reviews (bodies trimmed after the reviewed-commit line).
REVIEW_4515_EE20 = {
    "user": CODEX,
    "submitted_at": "2026-09-30T00:37:37Z",
    "commit_id": "ee20d0cc0c1489ee45b0440813188424251e539c",
    "state": "COMMENTED",
    "body": "\n### 💡 Codex Review\n\nHere are some automated review suggestions for this "
    "pull request.\n\n**Reviewed commit:** `ee20d0cc0c`\n",
}
REVIEW_4515_8FC0 = {
    **REVIEW_4515_EE20,
    "submitted_at": "2026-09-30T01:31:01Z",
    "commit_id": "8fc0eb2cd2af2b27b030d6920e2afffc1aba5ff1",
    "body": "\n### 💡 Codex Review\n\n**Reviewed commit:** `8fc0eb2cd2`\n",
}
#: The summary comment, edited in place; its table row as of 01:31:05Z.
SUMMARY_4515 = {
    "user": CODEX,
    "created_at": "2026-09-30T00:34:02Z",
    "body": "<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n"
    "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
    "| 📝 **Code Review** | ✅ **Completed** <relative-time "
    'datetime="2026-09-30T01:31:04.922748Z">2026-09-30T01:31:04.922748Z</relative-time> '
    "| `8fc0eb2` | New commits |\n",
}
#: The notice Codex left at 07:51:38Z, answering head d7812c36c8. The PR's
#: final head ff2936d255 was pushed at 14:17:23Z, SIX HOURS LATER.
OLD_NOTICE_4515 = {**NOTICE_4538, "created_at": "2026-09-30T07:51:38Z"}
HEAD_4515_FINAL = "ff2936d255e6870075bff60d4aa04cfacab4a5b0"
PUSH_4515_FINAL = datetime(2026, 9, 30, 14, 17, 23, tzinfo=UTC)
HEAD_4515_8FC0 = REVIEW_4515_8FC0["commit_id"]
PUSH_4515_8FC0 = datetime(2026, 9, 30, 1, 27, 23, tzinfo=UTC)

SMALL_DIFF = 40_000


def _state(head: str, pushed: datetime, reviews=(), comments=(), diff=SMALL_DIFF) -> HeadState:
    return HeadState(head, pushed, codex_artifacts(list(reviews), list(comments)), diff)


def _state_4538(diff: int = SMALL_DIFF) -> HeadState:
    return _state(
        HEAD_4538, pushed_at_from_check_suites(SUITES_4538), comments=[NOTICE_4538], diff=diff
    )


def _minutes_after(start: datetime, minutes: float) -> datetime:
    return start + timedelta(minutes=minutes)


# ----- the notice ----------------------------------------------------------


def test_a_notice_six_seconds_after_the_push_runs_sol() -> None:
    """#4538 as it happened. Ignoring the notice would leave this at WAIT for
    20 minutes, the delay this module exists to remove."""
    state = _state_4538()
    decision = decide(state, _minutes_after(state.pushed_at, 1))
    assert decision.action is Action.RUN_SOL
    assert "usage-limit notice" in decision.reason


def test_a_notice_left_for_an_earlier_head_does_not_run_sol() -> None:
    """#4515's 07:51Z notice answered an older head. Read as current, it would
    skip Codex's grace on every push that follows a walled one."""
    state = _state(HEAD_4515_FINAL, PUSH_4515_FINAL, comments=[OLD_NOTICE_4515])
    decision = decide(state, _minutes_after(PUSH_4515_FINAL, 5))
    assert decision.action is Action.WAIT


def test_an_old_notice_still_yields_to_silence_once_the_grace_runs_out() -> None:
    state = _state(HEAD_4515_FINAL, PUSH_4515_FINAL, comments=[OLD_NOTICE_4515])
    decision = decide(state, _minutes_after(PUSH_4515_FINAL, 25))
    assert decision.action is Action.RUN_SOL
    assert "silent 25 min" in decision.reason


def test_the_push_time_is_the_earliest_check_suite_not_the_commit_date() -> None:
    assert pushed_at_from_check_suites(SUITES_4538) == datetime(2026, 9, 30, 21, 14, 16, tzinfo=UTC)


def test_a_head_with_no_check_suite_is_unmeasured_not_undated() -> None:
    with pytest.raises(FallbackError, match="push time is unknown"):
        pushed_at_from_check_suites([{"total_count": 0, "check_suites": []}])


def test_an_impostor_login_cannot_post_the_notice() -> None:
    impostor = {**NOTICE_4538, "user": {"login": "chatgpt-codex-connector-attacker"}}
    state = _state(HEAD_4538, pushed_at_from_check_suites(SUITES_4538), comments=[impostor])
    assert decide(state, _minutes_after(state.pushed_at, 1)).action is Action.WAIT


# ----- silence -------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(19, Action.WAIT), (19.99, Action.WAIT), (20, Action.RUN_SOL), (21, Action.RUN_SOL)],
)
def test_silence_waits_inside_the_grace_and_runs_sol_past_it(
    minutes: float, expected: Action
) -> None:
    state = _state(HEAD_4515_FINAL, PUSH_4515_FINAL)
    assert decide(state, _minutes_after(PUSH_4515_FINAL, minutes)).action is expected


def test_the_grace_is_twenty_minutes() -> None:
    assert timedelta(minutes=20) == CFG.CODEX_GRACE


# ----- Codex actually reviewed ---------------------------------------------


def test_a_codex_review_at_the_head_is_covered_even_after_a_notice() -> None:
    state = _state(
        HEAD_4515_8FC0,
        PUSH_4515_8FC0,
        reviews=[REVIEW_4515_8FC0],
        comments=[{**NOTICE_4538, "created_at": "2026-09-30T01:28:00Z"}],
    )
    assert decide(state, _minutes_after(PUSH_4515_8FC0, 30)).action is Action.COVERED


def test_a_codex_review_of_an_earlier_head_covers_nothing() -> None:
    state = _state(HEAD_4515_8FC0, PUSH_4515_8FC0, reviews=[REVIEW_4515_EE20])
    assert decide(state, _minutes_after(PUSH_4515_8FC0, 2)).action is Action.WAIT
    assert decide(state, _minutes_after(PUSH_4515_8FC0, 30)).action is Action.RUN_SOL


def test_the_summary_rows_completed_status_covers_a_no_findings_head() -> None:
    """Codex reacts 👍 instead of reviewing when it finds nothing; the summary
    row marked Completed for the head is then the only artifact."""
    state = _state(HEAD_4515_8FC0, PUSH_4515_8FC0, comments=[SUMMARY_4515])
    assert decide(state, _minutes_after(PUSH_4515_8FC0, 30)).action is Action.COVERED
    other = _state(HEAD_4515_FINAL, PUSH_4515_FINAL, comments=[SUMMARY_4515])
    assert decide(other, _minutes_after(PUSH_4515_FINAL, 30)).action is Action.RUN_SOL


def test_codex_artifacts_keeps_only_codex_and_reads_both_kinds() -> None:
    human = {"user": {"login": "someone"}, "created_at": "2026-09-30T01:00:00Z", "body": "x"}
    found = codex_artifacts([REVIEW_4515_EE20], [human, SUMMARY_4515, OLD_NOTICE_4515])
    assert [a.kind for a in found] == ["review", "comment", "comment"]
    assert found[0].commit_id == REVIEW_4515_EE20["commit_id"]
    assert found[2].at == datetime(2026, 9, 30, 7, 51, 38, tzinfo=UTC)


# ----- the size limit ------------------------------------------------------


def test_the_limit_is_read_from_both_lanes_not_restated() -> None:
    lanes = min(sol_review.CFG.MAX_DIFF_BYTES, claude_review.CFG.MAX_DIFF_BYTES)
    assert lanes == CFG.MAX_DIFF_BYTES


def test_a_diff_over_the_lanes_limit_is_refused_not_run() -> None:
    state = _state_4538(diff=CFG.MAX_DIFF_BYTES + 1)
    decision = decide(state, _minutes_after(state.pushed_at, 1))
    assert decision.action is Action.REFUSED_OVERSIZE
    assert str(CFG.MAX_DIFF_BYTES) in decision.reason


def test_a_diff_at_the_limit_still_runs_sol() -> None:
    state = _state_4538(diff=CFG.MAX_DIFF_BYTES)
    assert decide(state, _minutes_after(state.pushed_at, 1)).action is Action.RUN_SOL


def test_an_oversize_diff_still_gives_codex_its_grace() -> None:
    state = _state(HEAD_4515_FINAL, PUSH_4515_FINAL, diff=CFG.MAX_DIFF_BYTES * 2)
    assert decide(state, _minutes_after(PUSH_4515_FINAL, 5)).action is Action.WAIT


# ----- Sol, then Claude, once ----------------------------------------------


def _after_notice(**lanes) -> Action:
    state = _state_4538()
    return decide(state, _minutes_after(state.pushed_at, 1), **lanes).action


def test_sol_exit_0_is_covered_and_never_runs_claude() -> None:
    assert _after_notice(sol=LaneResult(0)) is Action.COVERED


def test_sol_exit_3_runs_claude() -> None:
    assert _after_notice(sol=LaneResult(3)) is Action.RUN_CLAUDE


def test_every_sol_seat_spent_runs_claude() -> None:
    assert _after_notice(sol=LaneResult(3, all_seats_spent=True)) is Action.RUN_CLAUDE


def test_claude_already_tried_at_this_head_is_exhausted_not_rerun() -> None:
    assert _after_notice(sol=LaneResult(3), claude_attempted=True) is Action.EXHAUSTED


def test_claude_exit_0_is_covered_and_exit_3_is_exhausted() -> None:
    sol = LaneResult(3)
    assert _after_notice(sol=sol, claude_attempted=True, claude=LaneResult(0)) is Action.COVERED
    assert _after_notice(sol=sol, claude_attempted=True, claude=LaneResult(3)) is Action.EXHAUSTED


@pytest.mark.parametrize("lane", ["sol", "claude"])
def test_a_lane_exit_that_is_neither_0_nor_3_is_an_error(lane: str) -> None:
    with pytest.raises(FallbackError, match="neither 0 nor 3"):
        _after_notice(
            sol=LaneResult(1 if lane == "sol" else 3),
            claude=LaneResult(2) if lane == "claude" else None,
        )


def test_sol_seat_spent_marker_is_sol_reviews_own_words() -> None:
    """If sol_review rewords its all-seats-spent error, this goes red rather
    than the fallback silently never recognizing it."""
    source = Path(sol_review.__file__).read_text(encoding="utf-8")
    assert f'"{CFG.SOL_ALL_SEATS_SPENT}.' in source


# ----- bad inputs are errors -----------------------------------------------


def test_a_naive_timestamp_is_refused() -> None:
    state = _state_4538()
    with pytest.raises(FallbackError, match="timezone-aware"):
        decide(state, datetime(2026, 9, 30, 21, 20))  # noqa: DTZ001 - naive on purpose


def test_a_push_after_now_is_clock_skew_not_a_wait() -> None:
    state = _state_4538()
    with pytest.raises(FallbackError, match="clock skew"):
        decide(state, state.pushed_at - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (Action.COVERED, 0),
        (Action.WAIT, 0),
        (Action.RUN_SOL, 0),
        (Action.RUN_CLAUDE, 0),
        (Action.REFUSED_OVERSIZE, 1),
        (Action.EXHAUSTED, 1),
    ],
)
def test_every_action_maps_to_an_exit_code(action: Action, code: int) -> None:
    """An uncovered head that needs a human must not exit 0; the table must be
    total, since a missing action would crash the sweep mid-run."""
    assert exit_code(Decision(action, "reason")) == code
    assert set(EXIT_FOR) == set(Action)


@pytest.mark.parametrize("argv", [[], ["4538", "--sweep"]])
def test_the_cli_needs_exactly_one_target(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


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
def test_a_gh_error_is_raised_never_read_as_no_notice(gh_authenticated: None) -> None:
    """Negative control: a PR that does not exist must raise, not come back
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
