"""REVIEW-20 / REVIEW-21: the rubric names this repo's blocking rules, and every
review lane runs at a pinned model and effort it can prove.

Found live Thu 8 Oct 2026 on PR #5968: Sol (seat default gpt-5.6-luna, medium)
and Grok (broker default effort low) both READ the full 40 KB diff and posted
0 findings, missing a real artist name in a provenance note and three dates
whose weekday was wrong. Grok's own reasoning noticed the name and filed it as
"incomplete scrubbing but not a correctness bug".

Each test below states the regression it catches as an if/then line.
"""

from __future__ import annotations

import json

import pytest

from scripts import review_gh, review_lane, sol_review
from scripts.review_gh import TriageError
from scripts.review_prompt import SOL_FENCE, ReviewPromptConfig, build_prompt
from scripts.review_subscription import CURSOR_LANE, GROK_LANE
from scripts.subscription_review import read_run_result, sb_argv

HEAD = "abae480279b5541dc23c5e861a358782fcd4ecc9"


@pytest.fixture(autouse=True)
def _sol_remote_seat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sol_review.REMOTE_SEAT_ENV, "nucbox-wsl")
    monkeypatch.setenv(sol_review.REMOTE_SEAT_HOSTNAMES_ENV, "NucBoxEVO-X2")


def _prompt(body: str) -> str:
    cfg = ReviewPromptConfig("run1", 10_000, 12, SOL_FENCE, [])
    return build_prompt("5968", HEAD, "finish invented-name scrub", "+diff", cfg, body=body)[0]


# ----- REVIEW-20: the rubric ----------------------------------------------


@pytest.mark.requirement("REVIEW-20")
def test_the_rubric_names_the_three_repo_blocking_rules() -> None:
    """[if] a reviewer is prompted [then] the rubric names OSSPUB-01, weekdays and intent, [else stop]."""
    prompt = _prompt("")
    assert "OSSPUB-01" in prompt
    assert "weekday contradicts" in prompt
    assert "does not do what the PR title and body say" in prompt


@pytest.mark.requirement("REVIEW-20")
def test_a_blocking_rule_hit_can_never_be_downgraded_to_a_nit() -> None:
    """[if] a blocking-rule hit looks cosmetic [then] the rubric still makes it P1, [else stop]."""
    prompt = " ".join(_prompt("").split())
    assert "is a finding at P1 or higher" in prompt
    assert 'never "just a nit"' in prompt
    assert 'never "incomplete but not a correctness bug"' in prompt


@pytest.mark.requirement("REVIEW-20")
def test_the_pr_body_reaches_the_reviewer() -> None:
    """[if] the PR has a body [then] the reviewer sees it for the intent check, [else stop]."""
    prompt = _prompt("Finishes the scrub: no real artist names remain.")
    assert "Finishes the scrub: no real artist names remain." in prompt
    assert "(the PR body is empty)" in _prompt("  \n")


# ----- REVIEW-21: Sol pins --------------------------------------------------


def _header(model: str, effort: str) -> str:
    return (
        "OpenAI Codex v0.159.2\n--------\nworkdir: /tmp\n"
        f"model: {model}\nprovider: openai\nreasoning effort: {effort}\n"
        "session id: 01a11a02-5d05-7673-8ab8-b000b0e8841b\n--------\nuser\n"
    )


@pytest.mark.requirement("REVIEW-21")
def test_the_codex_argv_pins_model_and_effort() -> None:
    """[if] Sol runs codex [then] the argv carries the pinned model and effort, [else stop]."""
    argv = sol_review._codex_argv()
    assert argv[argv.index("-m") + 1] == sol_review.CFG.MODEL
    assert f"model_reasoning_effort={sol_review.CFG.EFFORT}" in argv
    remote = sol_review._seat_argv("agentbox", "NucBoxEVO-X2")[-1]
    assert f"-m {sol_review.CFG.MODEL}" in remote


@pytest.mark.requirement("REVIEW-21")
def test_a_header_at_the_pinned_config_is_accepted() -> None:
    """[if] codex ran at the pinned model and effort [then] the run is accepted, [else stop]."""
    out = _header(sol_review.CFG.MODEL, sol_review.CFG.EFFORT) + "+model: gpt-5.6-luna\n"
    assert sol_review.require_pinned_config(out) == sol_review.CFG.MODEL


@pytest.mark.requirement("REVIEW-21")
@pytest.mark.parametrize(
    "model,effort",
    [("gpt-5.6-luna", sol_review.CFG.EFFORT), (sol_review.CFG.MODEL, "low"), ("gpt-5.6-luna", "medium")],
    ids=["seat-default-model", "low-effort", "the-5968-config"],
)
def test_a_header_at_any_other_config_is_refused(model: str, effort: str) -> None:
    """[if] codex ran at another model or effort [then] the lane refuses it, [else stop]."""
    with pytest.raises(TriageError, match="pins"):
        sol_review.require_pinned_config(_header(model, effort))


@pytest.mark.requirement("REVIEW-21")
def test_a_run_with_no_header_is_refused() -> None:
    """[if] codex printed no header [then] the pin check refuses the run, [else stop]."""
    with pytest.raises(TriageError):
        sol_review.require_pinned_config("<<<SOL_JSON\n{}\nSOL_JSON>>>\n")


# ----- REVIEW-21: Sol token log line ----------------------------------------


def _rollout(**usage: int) -> str:
    events = [
        {"type": "session_meta", "payload": {"id": "x"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage}}},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.mark.requirement("REVIEW-21")
def test_the_token_line_separates_input_cached_and_output() -> None:
    """[if] the #5968 rollout is read [then] input, cached and output print apart, [else stop]."""
    usage = sol_review.token_usage_from_rollout(
        _rollout(input_tokens=38155, cached_input_tokens=37632, output_tokens=545, reasoning_output_tokens=516)
    )
    assert usage.line() == "input=38155 cached=37632 output=545 reasoning=516"


@pytest.mark.requirement("REVIEW-21")
def test_an_unreadable_token_record_is_unknown_never_a_number() -> None:
    """[if] no token record can be read [then] the line says UNKNOWN, not a number, [else stop]."""
    with pytest.raises(TriageError, match="no token_count"):
        sol_review.token_usage_from_rollout('{"type": "session_meta", "payload": {}}\n')
    assert sol_review.token_breakdown("local", "no header here").startswith("UNKNOWN")


# ----- REVIEW-21: Grok and Cursor pins --------------------------------------


@pytest.mark.requirement("REVIEW-21")
@pytest.mark.parametrize("lane", [GROK_LANE, CURSOR_LANE], ids=["grok", "cursor"])
def test_the_broker_argv_pins_model_and_effort(lane, tmp_path) -> None:
    """[if] a broker lane runs [then] sb gets the pinned model and effort, [else stop]."""
    argv = sb_argv(lane, "5968", tmp_path / "p.md", tmp_path)
    assert argv[argv.index("--model") + 1] == lane.model
    assert argv[argv.index("--effort") + 1] == lane.effort


def _result(lane, *, model: str, effort: str, reported: list[str]) -> str:
    return json.dumps(
        {
            "metered": False,
            "request": {"model": model, "effort": effort},
            "parsed": {"text": "reply", "models_used": reported},
        }
    )


@pytest.mark.requirement("REVIEW-21")
def test_a_run_at_the_pinned_config_is_accepted() -> None:
    """[if] sb ran at the pinned model and effort [then] the reply is accepted, [else stop]."""
    stdout = _result(GROK_LANE, model=GROK_LANE.model, effort=GROK_LANE.effort, reported=["grok-4.7-build"])
    assert read_run_result(GROK_LANE, stdout) == ("reply", "grok-4.7-build")


@pytest.mark.requirement("REVIEW-21")
@pytest.mark.parametrize(
    "model,effort,reported",
    [
        ("grok-4.6", "low", ["grok-4.6-build"]),
        (GROK_LANE.model, "low", ["grok-4.7-build"]),
        (GROK_LANE.model, GROK_LANE.effort, ["grok-4.6-build"]),
    ],
    ids=["the-5968-config", "low-effort", "answered-by-another-model"],
)
def test_a_run_at_any_other_config_is_refused(model: str, effort: str, reported: list[str]) -> None:
    """[if] sb ran another model, effort or answerer [then] the lane refuses it, [else stop]."""
    with pytest.raises(TriageError):
        read_run_result(GROK_LANE, _result(GROK_LANE, model=model, effort=effort, reported=reported))


# ----- the replay switch ------------------------------------------------------


def test_a_replayed_head_is_refused_unless_the_run_posts_nothing(monkeypatch) -> None:
    """[if] --at-sha replays an earlier head [then] only a dry run is allowed, [else stop]."""

    def must_not_pin(_pr: str) -> str:
        raise AssertionError("a replay must not read the live head")

    monkeypatch.setattr(review_lane, "pinned_head", must_not_pin)
    monkeypatch.setattr(review_lane, "pr_commit_shas", lambda _pr: frozenset({HEAD}))
    assert review_lane.review_head("5968", HEAD, dry_run=True) == HEAD
    with pytest.raises(TriageError, match="not a commit of PR"):
        review_lane.review_head("5968", "c" * 40, dry_run=True)
    with pytest.raises(TriageError, match="--dry-run"):
        review_lane.review_head("5968", HEAD, dry_run=False)
    with pytest.raises(TriageError, match="40-hex"):
        review_lane.review_head("5968", HEAD[:12], dry_run=True)


@pytest.mark.requirement("REVIEW-20")
def test_the_identity_rule_scopes_out_attribution_and_machine_nicknames() -> None:
    """[if] the identity rule is read [then] attribution names and machine nicknames are excluded, [else stop]."""
    prompt = " ".join(_prompt("").split())
    assert "Out of scope: the maintainer's first name in an attribution" in prompt
    assert "the fleet's machine nicknames" in prompt
    assert "a real person's name" not in prompt, "the PR #6010 overshoot: every attribution became a P1"


def test_a_replay_with_a_null_merge_base_is_refused(monkeypatch) -> None:
    """[if] the compare API answers null [then] the replay refuses rather than diffing from "null"."""
    from scripts import review_pr_diff

    monkeypatch.setattr(review_gh, "_gh", lambda _args: '{"base": {"sha": "' + "b" * 40 + '"}}')
    monkeypatch.setattr(review_pr_diff, "_gh", lambda _args: "null\n")
    with pytest.raises(TriageError, match="no merge base"):
        review_pr_diff.pr_diff_at("o/r", "5968", HEAD)


@pytest.mark.parametrize(
    "view",
    ['{"body": "b"}', '{"title": null, "body": "b"}', '{"title": "", "body": "b"}'],
    ids=["title-missing", "title-null", "title-empty"],
)
def test_a_pr_view_without_a_usable_title_and_body_is_refused(monkeypatch, view: str) -> None:
    """[if] the REST pull has no title [then] the lane refuses rather than prompt with a blank."""
    monkeypatch.setattr(review_gh, "_gh", lambda _args: view)
    with pytest.raises(TriageError, match="no usable title and body"):
        review_lane.pr_title_and_body("6010")


def test_an_empty_body_is_a_real_answer(monkeypatch) -> None:
    """[if] the author wrote no body (REST sends null) [then] the lane still runs with an empty body."""
    monkeypatch.setattr(review_gh, "_gh", lambda _args: '{"title": "t", "body": null}')
    assert review_lane.pr_title_and_body("6010") == ("t", "")
