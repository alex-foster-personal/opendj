"""Do the Grok and Cursor lanes produce something the merge gate can read, and
can nobody else forge it?

Same two-direction discipline as test_review_sol.py: every identity rule has a
case that must count and a case that must not, because a matcher that rejects
everything passes every forgery test while making the gate permanently MISS.
The broker is driven through a REAL `sb` stub executable that records its own
argv, so a test that never reached the subprocess cannot pass.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from scripts import subscription_review
from scripts.review_coverage import (
    CHECKLESS_REVIEWERS,
    EXPECTED_REVIEWERS,
    _collect_evidence,
    classify_reviewer,
)
from scripts.review_coverage_carry import reviewed_shas_for_reviewer
from scripts.review_gh import TriageError
from scripts.review_lane import Finding, cli_lane_marker_skipped_mismatch, parse_findings
from scripts.review_prompt import ALL_FENCES, CURSOR_FENCE, GROK_FENCE, SOL_FENCE
from scripts.review_sol import is_sol_artifact, substitute_alternatives
from scripts.review_sol import marker as sol_marker
from scripts.review_subscription import (
    CURSOR,
    CURSOR_LANE,
    GROK,
    GROK_LANE,
    SUBSCRIPTION_LANES,
)
from scripts.review_thread_parse import build_thread
from scripts.subscription_review import (
    read_run_result,
    review_with_broker,
    sb_argv,
    summary_body,
)

HEAD = "84abf19dd247ef79b1e81452c17df124d883d4d8"
OLD = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
MAINTAINER = "maintainer"
GROK_MARK = GROK_LANE.marker(HEAD, "grok-4.6-build")
CURSOR_MARK = CURSOR_LANE.marker(HEAD, "requested:composer-2.5")


def _finding(severity: str = "P1", line: int | None = 12) -> Finding:
    return Finding("scripts/thing.py", line, severity, "do not swallow the error", "detail")


# ----- identity: login AND marker AND head --------------------------------


@pytest.mark.parametrize("lane", SUBSCRIPTION_LANES, ids=lambda lane: lane.slug)
def test_a_real_lane_artifact_at_the_head_counts(lane) -> None:
    assert lane.is_artifact(MAINTAINER, f"review\n\n{lane.marker(HEAD, 'm')}", HEAD) is True
    assert lane.is_artifact(f"{MAINTAINER}[bot]", lane.marker(HEAD[:10], "m"), HEAD) is True


@pytest.mark.parametrize("lane", SUBSCRIPTION_LANES, ids=lambda lane: lane.slug)
def test_a_marker_from_an_untrusted_login_does_not_count(lane) -> None:
    for forger in ("some-drive-by", f"{MAINTAINER}-attacker", "chatgpt-codex-connector[bot]"):
        assert lane.is_artifact(forger, lane.marker(HEAD, "m"), HEAD) is False
        assert lane.is_thread(forger, lane.marker(HEAD, "m")) is False


@pytest.mark.parametrize("lane", SUBSCRIPTION_LANES, ids=lambda lane: lane.slug)
def test_the_trusted_login_alone_does_not_count(lane) -> None:
    assert lane.is_artifact(MAINTAINER, "LGTM, reviewed by Grok", HEAD) is False
    assert lane.is_thread(MAINTAINER, "nit: rename this") is False


@pytest.mark.parametrize("lane", SUBSCRIPTION_LANES, ids=lambda lane: lane.slug)
def test_a_marker_naming_an_earlier_push_does_not_count(lane) -> None:
    assert lane.is_artifact(MAINTAINER, lane.marker(OLD, "m"), HEAD) is False
    assert lane.is_thread(MAINTAINER, lane.marker(OLD, "m")) is True, "authorship holds at any head"


def test_a_near_miss_marker_is_not_half_accepted() -> None:
    for body in (
        f"<!-- grok-review v2 sha={HEAD} model=m -->",
        f"<!-- grok-review v1 model=m sha={HEAD} -->",
        "<!-- grok-review v1 sha=not-a-sha model=m -->",
        f"grok-review v1 sha={HEAD} model=m",
    ):
        assert GROK_LANE.is_artifact(MAINTAINER, body, HEAD) is False, body


def test_one_lane_s_marker_never_certifies_another() -> None:
    assert CURSOR_LANE.is_artifact(MAINTAINER, GROK_MARK, HEAD) is False
    assert GROK_LANE.is_artifact(MAINTAINER, CURSOR_MARK, HEAD) is False
    assert is_sol_artifact(MAINTAINER, GROK_MARK, HEAD) is False
    assert GROK_LANE.is_artifact(MAINTAINER, sol_marker(HEAD, "local", "gpt"), HEAD) is False


# ----- the coverage gate reads it -----------------------------------------


def _review(login: str, body: str, sha: str = HEAD) -> dict:
    return {"user": {"login": login}, "body": body, "commit_id": sha}


def test_grok_and_cursor_are_expected_checkless_alternatives() -> None:
    assert {GROK, CURSOR} <= set(EXPECTED_REVIEWERS)
    assert {GROK, CURSOR} <= CHECKLESS_REVIEWERS
    assert EXPECTED_REVIEWERS[-1] == "Claude", "Claude stays last: it bills the maintainer's allowance"


@pytest.mark.requirement("REVIEW-15")
@pytest.mark.parametrize("lane", SUBSCRIPTION_LANES, ids=lambda lane: lane.slug)
def test_a_lane_review_satisfies_coverage_and_a_forgery_does_not(lane) -> None:
    """[if] a forged or stale lane marker is posted [then] coverage misses, [else stop]."""
    good = _collect_evidence(lane.name, [_review(MAINTAINER, lane.marker(HEAD, "m"))], [], [], HEAD)
    assert classify_reviewer(lane.name, [], good).reviewed is True
    forged = _collect_evidence(lane.name, [_review("drive-by", lane.marker(HEAD, "m"))], [], [], HEAD)
    assert classify_reviewer(lane.name, [], forged).reviewed is False
    stale = _collect_evidence(lane.name, [_review(MAINTAINER, lane.marker(OLD, "m"), OLD)], [], [], HEAD)
    assert classify_reviewer(lane.name, [], stale).reviewed is False


def _verdicts(reviewed: set[str]) -> list:
    out = []
    for name in EXPECTED_REVIEWERS:
        lane = next((x for x in SUBSCRIPTION_LANES if x.name == name), None)
        reviews = [_review(MAINTAINER, lane.marker(HEAD, "m"))] if lane and name in reviewed else []
        out.append(classify_reviewer(name, [], _collect_evidence(name, reviews, [], [], HEAD)))
    return substitute_alternatives(out, EXPECTED_REVIEWERS)


def test_grok_alone_covers_the_set_and_the_board_says_so() -> None:
    verdicts = {v.name: v for v in _verdicts({GROK})}
    assert all(v.reviewed for v in verdicts.values())
    assert verdicts[GROK].substituted_by == ""
    assert verdicts["Codex"].substituted_by == GROK
    assert verdicts["Claude"].substituted_by == GROK


def test_nobody_reviewing_still_fails_with_five_reviewers() -> None:
    assert [v.reviewed for v in _verdicts(set())] == [False] * len(EXPECTED_REVIEWERS)


def test_carry_reads_the_sha_out_of_a_lane_marker() -> None:
    issue = [{"user": {"login": MAINTAINER}, "body": CURSOR_LANE.marker(OLD, "m")}]
    shas = reviewed_shas_for_reviewer(CURSOR, [], [], issue, matches_login=lambda *_: False)
    assert shas == (OLD,)
    forged = [{"user": {"login": "drive-by"}, "body": CURSOR_LANE.marker(OLD, "m")}]
    assert reviewed_shas_for_reviewer(CURSOR, [], [], forged, matches_login=lambda *_: False) == ()


def test_a_skipped_mismatch_is_checked_for_subscription_lanes() -> None:
    diff = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n+x\n"
    lying = GROK_LANE.marker(HEAD, "m", skipped=frozenset({"reqs.json"}))
    assert cli_lane_marker_skipped_mismatch(GROK, lying, diff) is not None
    assert cli_lane_marker_skipped_mismatch(GROK, GROK_MARK, diff) is None


# ----- threads: governed by the three-state rule ---------------------------


def _node(login: str, body: str) -> dict:
    comment = {"author": {"login": login}, "body": body, "url": "u", "createdAt": "2026-10-01T03:00:00Z"}
    return {"id": "T", "path": "scripts/thing.py", "line": 12, "isResolved": False,
            "isOutdated": False, "comments": {"nodes": [comment]}}


@pytest.mark.parametrize("lane", SUBSCRIPTION_LANES, ids=lambda lane: lane.slug)
def test_a_lane_thread_round_trips_with_its_severity_and_verdict(lane) -> None:
    thread = build_thread(_node(MAINTAINER, _finding("P1").body(lane.marker(HEAD, "m"))))
    assert thread is not None
    assert thread.bot == lane.name
    assert (thread.severity, thread.blocking) == ("P1", "BLOCKING")
    assert build_thread(_node("drive-by", _finding("P0").body(lane.marker(HEAD, "m")))) is None


# ----- the reply contract --------------------------------------------------


def test_each_lane_has_its_own_fence_and_all_are_skipped_by_the_fallback() -> None:
    assert len({GROK_FENCE, CURSOR_FENCE, SOL_FENCE}) == 3
    assert {GROK_FENCE, CURSOR_FENCE} <= set(ALL_FENCES)


def test_a_grok_fenced_block_is_not_cursor_s_answer() -> None:
    run = "abc123def456"
    reply = f'{GROK_FENCE.open}\n{{"run": "{run}", "findings": []}}\n{GROK_FENCE.close}'
    assert parse_findings(reply, run, GROK_FENCE, 12) == []
    with pytest.raises(TriageError):
        parse_findings(reply, run, CURSOR_FENCE, 12)


def test_the_summary_carries_a_marker_its_own_lane_recognizes() -> None:
    body = summary_body(CURSOR_LANE, HEAD, "requested:composer-2.5", [_finding("P0")], [], [])
    assert CURSOR_LANE.is_artifact(MAINTAINER, body, HEAD) is True
    assert "P0 1" in body and "requested:composer-2.5" in body


# ----- the broker: real subprocess stub -----------------------------------


def _run_result(text: str, *, metered: object = False, models: list[str] | None = None) -> str:
    return json.dumps({"metered": metered, "parsed": {"text": text, "models_used": models or []}})


def test_the_model_is_reported_or_flagged_as_only_requested() -> None:
    assert read_run_result(GROK_LANE, _run_result("x", models=["grok-4.6-build"]))[1] == "grok-4.6-build"
    assert read_run_result(CURSOR_LANE, _run_result("x"))[1] == "requested:composer-2.5"


@pytest.mark.parametrize(
    "stdout",
    [_run_result("x", metered=True), _run_result("x", metered=None), _run_result("  "), "not json"],
    ids=["metered", "metered-unknown", "empty-reply", "no-json"],
)
@pytest.mark.requirement("REVIEW-15")
def test_a_run_that_is_not_a_clean_subscription_review_is_refused(stdout: str) -> None:
    """[if] the broker run is metered, empty or unparseable [then] refuse, [else stop]."""
    with pytest.raises(TriageError):
        read_run_result(GROK_LANE, stdout)


def test_the_broker_argv_is_read_only_subscription_and_attributed(tmp_path: Path) -> None:
    argv = sb_argv(CURSOR_LANE, "42", tmp_path / "p.md", tmp_path)
    assert argv[argv.index("--access") + 1] == "read"
    assert argv[argv.index("--provider") + 1] == "cursor"
    assert argv[argv.index("--model") + 1] == "composer-2.5"
    assert argv[argv.index("--pr") + 1] == "42"
    assert "--allow-metered" not in argv and "--json" in argv


def _stub_sb(tmp_path: Path, rc: int, stdout: str, stderr: str = "") -> Path:
    record = tmp_path / "sb-argv.json"
    stub = tmp_path / "sb"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({rc})\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_a_broker_refusal_is_a_triage_error_naming_the_error_line(tmp_path, monkeypatch) -> None:
    stub = _stub_sb(tmp_path, 2, "", "noise\n[ERROR] BillingGuardError: grok is not logged in\n")
    monkeypatch.setattr(subscription_review.CFG, "SB", str(stub))
    with pytest.raises(TriageError, match="BillingGuardError: grok is not logged in"):
        review_with_broker(GROK_LANE, "42", "prompt")
    ran = json.loads((tmp_path / "sb-argv.json").read_text())
    assert ran[:5] == ["run", "--provider", "grok", "--model", "grok-4.6"], "the stub must have run"


def test_a_successful_broker_run_returns_the_reply(tmp_path, monkeypatch) -> None:
    stub = _stub_sb(tmp_path, 0, _run_result("REPLY", models=["grok-4.6-build"]))
    monkeypatch.setattr(subscription_review.CFG, "SB", str(stub))
    assert review_with_broker(GROK_LANE, "42", "prompt") == ("REPLY", "grok-4.6-build")
    ran = json.loads((tmp_path / "sb-argv.json").read_text())
    assert Path(ran[ran.index("--prompt-file") + 1]).name == "prompt.md"


def test_a_could_not_review_run_exits_3_never_0(monkeypatch, capsys) -> None:
    def refuse(*_args, **_kwargs) -> int:
        raise TriageError("quota wall")

    monkeypatch.setattr(subscription_review, "run", refuse)
    assert subscription_review.main(["grok", "42"]) == 3
    assert "[grok-review] FAILED: quota wall" in capsys.readouterr().err
