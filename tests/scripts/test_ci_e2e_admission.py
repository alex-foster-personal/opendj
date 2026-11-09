"""scripts/ci_e2e_admission.py: a workflow_run of CI is admitted only at a green `ci gate`
on the pull request's CURRENT head, pinned to its merge commit, and its verdict is
published on that head (ADR-NEW-e2e-after-ci-gate, PR #8538 review round 2)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_e2e_admission import (
    MERGEABILITY_ATTEMPTS,
    OPT_IN_LABEL,
    OPT_IN_LABEL_ENV,
    PUBLISHED_CHECK_NAME,
    Admission,
    AdmissionError,
    PublishInputs,
    RunEvent,
    gate_state,
    main,
    publish_payload,
    resolve_admission,
    write_outputs,
)
from scripts.ci_wait_workflows import WORKFLOW_RUN_OPT_IN_LABELS, WORKFLOW_RUN_PUBLISHED_CHECKS

REPO = "private_owner/music-dj-tools"
HEAD = "74a2372b29e1c0c6d8f2b4a5c6d7e8f9a0b1c2d3"
MERGE = "1f0a9310874324fe6ca32f9daaa27bc40afa2016"
GATE_PATH = f"repos/{REPO}/commits/{HEAD}/check-runs?check_name=ci%20gate&per_page=100"
PULL_PATH = f"repos/{REPO}/pulls/6474"

PR_RUN = RunEvent(
    repository=REPO,
    event_name="workflow_run",
    run_event="pull_request",
    run_url="https://github.com/x/y/actions/runs/41804607987",
    head_sha=HEAD,
    pr_number="6474",
    pr_base="main",
)


def _gate(status: str, conclusion: str | None, completed_at: str = "2026-11-04T09:23:35Z") -> dict:
    return {"name": "ci gate", "status": status, "conclusion": conclusion, "completed_at": completed_at}


def _pull(**overrides: Any) -> dict:
    pull: dict[str, Any] = {
        "head": {"sha": HEAD},
        "state": "open",
        "merged": False,
        "mergeable": True,
        "merge_commit_sha": MERGE,
        "labels": [{"name": "ci:nightly"}],
    }
    pull.update(overrides)
    return pull


class RecordedApi:
    """A GET-only GitHub API answering from a script, counting every call."""

    def __init__(self, responses: dict[str, Any] | None = None, pulls: list[dict] | None = None) -> None:
        self.responses = dict(responses or {})
        self.pulls = list(pulls or [])
        self.calls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if path == PULL_PATH and self.pulls:
            return self.pulls.pop(0)
        if path not in self.responses:
            raise AssertionError(f"unexpected API call {path}")
        return self.responses[path]


def _no_sleep(_: float) -> None:
    return None


# ----- gate_state ----------------------------------------------------------------


def test_gate_state_reads_the_latest_completed_run() -> None:
    """if an older green gate outranks a newer red one then a red head is admitted"""
    runs = [
        _gate("completed", "success", "2026-11-04T08:00:00Z"),
        _gate("completed", "failure", "2026-11-04T09:00:00Z"),
    ]
    assert gate_state(runs) == "completed failure"
    assert gate_state([]) == "absent absent"
    assert gate_state([_gate("in_progress", None, "")]) == "in_progress None"


# ----- resolve_admission ---------------------------------------------------------


def test_a_non_workflow_run_event_passes_through_without_an_api_call() -> None:
    """if a push to main is scoped as a pull request then main's E2E checks out nothing"""
    api = RecordedApi()
    for event_name in ("push", "schedule", "workflow_dispatch"):
        admission = resolve_admission(replace(PR_RUN, event_name=event_name), api, _no_sleep)
        assert admission == Admission(scope_event=event_name, checkout_ref="", repair=False, pr="", skip="")
    assert api.calls == []


@pytest.mark.parametrize(
    ("event", "fragment"),
    [
        (replace(PR_RUN, run_event="push"), "was a push run, not a pull request"),
        (replace(PR_RUN, pr_number=""), "names no pull request"),
        (replace(PR_RUN, pr_base="windows-parity-rc"), "targets windows-parity-rc, not af--b2b-link or main"),
    ],
)
def test_a_run_that_is_not_a_main_pull_request_is_skipped_before_any_api_call(event: RunEvent, fragment: str) -> None:
    """if a push run's CI completion is admitted then main's tip gets a second E2E on the PR seats"""
    api = RecordedApi()
    admission = resolve_admission(event, api, _no_sleep)
    assert admission.scope_event == "workflow_run" and admission.checkout_ref == ""
    assert fragment in admission.skip
    assert api.calls == []


@pytest.mark.parametrize(
    "runs",
    [[], [_gate("completed", "failure")], [_gate("in_progress", None, "")], [_gate("completed", "cancelled")]],
)
def test_a_head_whose_gate_is_not_green_is_skipped_without_reading_the_pull(runs: list[dict]) -> None:
    """if a red gate's head is admitted then a red head spends an e2e seat"""
    api = RecordedApi({GATE_PATH: {"check_runs": runs}})
    admission = resolve_admission(PR_RUN, api, _no_sleep)
    assert admission.checkout_ref == "" and admission.pr == "6474"
    assert f"ci gate at {HEAD[:10]} is '{gate_state(runs)}', not green" in admission.skip
    assert api.calls == [GATE_PATH]


def test_a_green_head_the_pull_request_has_moved_past_is_skipped() -> None:
    """if a superseded head is admitted then scope reads one revision and the gate tests another"""
    newer = "9" * 40
    api = RecordedApi({GATE_PATH: {"check_runs": [_gate("completed", "success")]}}, pulls=[_pull(head={"sha": newer})])
    admission = resolve_admission(PR_RUN, api, _no_sleep)
    assert admission.checkout_ref == ""
    assert f"head moved from {HEAD[:10]} to {newer[:10]}" in admission.skip
    assert api.calls == [GATE_PATH, PULL_PATH]


def test_an_unmergeable_pull_request_is_skipped() -> None:
    """if a conflicting pull request is admitted then the checkout of a missing merge commit reads as a red E2E"""
    api = RecordedApi({GATE_PATH: {"check_runs": [_gate("completed", "success")]}}, pulls=[_pull(mergeable=False)])
    admission = resolve_admission(PR_RUN, api, _no_sleep)
    assert admission.checkout_ref == ""
    assert "is not mergeable into main" in admission.skip


@pytest.mark.parametrize(("merged", "fragment"), [(True, "is already merged"), (False, "is closed")])
def test_a_pull_request_that_is_no_longer_open_is_skipped_without_polling(merged: bool, fragment: str) -> None:
    """if a merged pull request is polled for mergeability then its E2E goes red after the lander merged it"""
    # GitHub answers `mergeable: null` for a closed pull request for good; the lander merges on a green
    # ci gate, often before this workflow_run starts (#8597, run 41995964780, Thu 5 Nov 2026).
    closed = _pull(state="closed", merged=merged, mergeable=None, merge_commit_sha=None)
    api = RecordedApi({GATE_PATH: {"check_runs": [_gate("completed", "success")]}}, pulls=[closed])
    admission = resolve_admission(PR_RUN, api, _no_sleep)
    assert admission.checkout_ref == ""
    assert fragment in admission.skip
    assert api.calls == [GATE_PATH, PULL_PATH]


def test_a_green_current_head_is_admitted_at_its_pinned_merge_commit() -> None:
    """if checkout_ref is the moving merge ref then two jobs of one run can test two revisions"""
    api = RecordedApi(
        {GATE_PATH: {"check_runs": [_gate("completed", "success")]}},
        pulls=[_pull(labels=[{"name": "ci:trunk-repair"}])],
    )
    admission = resolve_admission(PR_RUN, api, _no_sleep)
    assert admission == Admission(scope_event="pull_request", checkout_ref=MERGE, repair=True, pr="6474", skip="")
    assert admission.outputs() == {
        "scope_event": "pull_request",
        "checkout_ref": MERGE,
        "repair": "true",
        "pr": "6474",
        "skip": "",
    }


# ----- opt-in label (ADR-NEW-e2e-opt-in-by-label) -------------------------------------


GREEN_GATE = {GATE_PATH: {"check_runs": [_gate("completed", "success")]}}
OPTED_IN = replace(PR_RUN, opt_in_label=OPT_IN_LABEL)


@pytest.mark.requirement("DEVOPS-10534")
def test_the_opt_in_label_is_the_one_ci_wait_reads() -> None:
    """[if] admission's label drifts from the ci-wait catalog [then] ci-wait owes a verdict admission skips, [else stop]."""
    assert WORKFLOW_RUN_OPT_IN_LABELS["e2e.yml"] == OPT_IN_LABEL == "e2e"
    assert OPT_IN_LABEL_ENV == "E2E_OPT_IN_LABEL"


@pytest.mark.requirement("DEVOPS-10534")
def test_without_the_env_every_green_head_is_admitted_as_before() -> None:
    """[if] an empty E2E_OPT_IN_LABEL gates admission [then] landing this script before the workflow skips every PR, [else stop]."""
    env = {"GITHUB_REPOSITORY": REPO, "EVENT_NAME": "workflow_run", "RUN_EVENT": "pull_request"}
    env |= {"HEAD_SHA": HEAD, "PR_NUMBER": "6474", "PR_BASE": "main"}
    event = RunEvent.from_env(env)
    assert event.opt_in_label == ""
    api = RecordedApi(GREEN_GATE, pulls=[_pull()])
    assert resolve_admission(event, api, _no_sleep).checkout_ref == MERGE
    assert RunEvent.from_env(env | {OPT_IN_LABEL_ENV: "e2e"}).opt_in_label == "e2e"


@pytest.mark.requirement("DEVOPS-10534")
def test_an_unlabelled_pull_request_is_skipped_before_mergeability_is_polled() -> None:
    """[if] an unlabelled green head is admitted [then] every pull request still spends an e2e seat, [else stop]."""
    api = RecordedApi(GREEN_GATE, pulls=[_pull(mergeable=None, merge_commit_sha=None)])
    admission = resolve_admission(OPTED_IN, api, _no_sleep)
    assert admission.scope_event == "workflow_run" and admission.checkout_ref == "" and admission.pr == "6474"
    assert "carries no `e2e` label" in admission.skip and "opt-in" in admission.skip
    assert api.calls == [GATE_PATH, PULL_PATH], "no mergeability poll for a head that cannot be admitted"


@pytest.mark.requirement("DEVOPS-10534")
def test_a_labelled_pull_request_is_admitted_at_its_merge_commit() -> None:
    """[if] the `e2e` label does not admit a green head [then] nothing can opt a pull request in, [else stop]."""
    api = RecordedApi(GREEN_GATE, pulls=[_pull(labels=[{"name": "ci:nightly"}, {"name": OPT_IN_LABEL}])])
    admission = resolve_admission(OPTED_IN, api, _no_sleep)
    assert admission == Admission(scope_event="pull_request", checkout_ref=MERGE, repair=False, pr="6474", skip="")


@pytest.mark.requirement("DEVOPS-10534")
def test_the_unlabelled_skip_publishes_a_skipped_verdict_with_the_reason() -> None:
    """[if] the opt-in skip publishes failure [then] every unlabelled head shows a red e2e verdict, [else stop]."""
    api = RecordedApi(GREEN_GATE, pulls=[_pull()])
    skip = resolve_admission(OPTED_IN, api, _no_sleep).skip
    payload = publish_payload(replace(PUBLISH, scope_event="workflow_run", skip=skip))
    assert payload is not None and payload["conclusion"] == "skipped"
    assert payload["output"]["summary"] == skip and "carries no `e2e` label" in skip


def test_mergeability_still_computing_is_polled_then_admitted() -> None:
    """if `mergeable: null` is read as unmergeable then every fresh push is skipped"""
    waits: list[float] = []
    api = RecordedApi(
        {GATE_PATH: {"check_runs": [_gate("completed", "success")]}},
        pulls=[_pull(mergeable=None, merge_commit_sha=None), _pull(mergeable=None, merge_commit_sha=None), _pull()],
    )
    admission = resolve_admission(PR_RUN, api, waits.append)
    assert admission.checkout_ref == MERGE
    assert len(waits) == 2 and api.calls.count(PULL_PATH) == 3


def test_mergeability_never_computed_goes_red_not_skipped() -> None:
    """if GitHub never answers mergeability and the run skips then a flake hides as a designed skip"""
    api = RecordedApi(
        {GATE_PATH: {"check_runs": [_gate("completed", "success")]}},
        pulls=[_pull(mergeable=None, merge_commit_sha=None) for _ in range(MERGEABILITY_ATTEMPTS)],
    )
    with pytest.raises(AdmissionError, match="has not computed mergeability"):
        resolve_admission(PR_RUN, api, _no_sleep)


def test_a_mergeable_pull_without_a_merge_sha_goes_red() -> None:
    """if an empty merge sha is pinned then every later checkout resolves main's sha instead"""
    api = RecordedApi({GATE_PATH: {"check_runs": [_gate("completed", "success")]}}, pulls=[_pull(merge_commit_sha="")])
    with pytest.raises(AdmissionError, match="names no merge_commit_sha"):
        resolve_admission(PR_RUN, api, _no_sleep)


# ----- publish_payload -------------------------------------------------------------


PUBLISH = PublishInputs(
    repository=REPO,
    run_event="pull_request",
    head_sha=HEAD,
    pr_number="6474",
    scope_result="success",
    run_conclusion="success",
    scope_event="pull_request",
    skip="",
    in_scope="true",
    verdict_outcome="success",
    run_url="https://github.com/x/y/actions/runs/41810000000",
    run_id="41810000000",
)


def test_the_published_check_is_the_name_ci_wait_expects_on_the_head() -> None:
    """if the published name drifts from the ci-wait catalog then ci-wait waits for a check no head carries"""
    assert WORKFLOW_RUN_PUBLISHED_CHECKS["e2e.yml"] == frozenset({PUBLISHED_CHECK_NAME})


def test_a_green_in_scope_run_publishes_success_on_the_triggering_head() -> None:
    """if the verdict lands on main's sha only then the pull request never shows e2e verdict"""
    payload = publish_payload(PUBLISH)
    assert payload is not None
    assert payload["name"] == PUBLISHED_CHECK_NAME and payload["head_sha"] == HEAD
    assert payload["status"] == "completed" and payload["conclusion"] == "success"
    assert payload["details_url"] == PUBLISH.run_url and payload["external_id"] == PUBLISH.run_id
    assert payload["output"]["title"] == "E2E green"


def test_an_out_of_scope_green_run_publishes_success_with_the_reason() -> None:
    """if a docs-only head gets no verdict then ci-wait times out on a docs-only pull request"""
    payload = publish_payload(replace(PUBLISH, in_scope="false"))
    assert payload is not None and payload["conclusion"] == "success"
    assert "out of scope" in payload["output"]["title"]


def test_a_red_gate_publishes_failure() -> None:
    """if a red gate publishes success then an advisory red reads green on the head"""
    payload = publish_payload(replace(PUBLISH, verdict_outcome="failure"))
    assert payload is not None and payload["conclusion"] == "failure"


def test_a_run_the_scope_job_did_not_admit_publishes_skipped_with_the_reason() -> None:
    """if a not-admitted head publishes success then a red ci gate's head carries a green e2e verdict"""
    reason = f"ci gate at {HEAD[:10]} is 'completed failure', not green"
    payload = publish_payload(replace(PUBLISH, scope_event="workflow_run", skip=reason, in_scope="false"))
    assert payload is not None and payload["conclusion"] == "skipped"
    assert payload["output"]["summary"] == reason


@pytest.mark.parametrize("ci_conclusion", ["cancelled", "failure"])
def test_a_scope_job_skipped_for_a_non_green_ci_run_publishes_skipped(ci_conclusion: str) -> None:
    """if a scope job skipped by the CI-conclusion guard publishes failure then every red CI head carries a red e2e verdict"""
    payload = publish_payload(
        replace(PUBLISH, scope_result="skipped", run_conclusion=ci_conclusion, scope_event="", skip="", in_scope="")
    )
    assert payload is not None and payload["conclusion"] == "skipped"
    assert f"CI concluded {ci_conclusion}" in payload["output"]["summary"]


def test_a_scope_job_skipped_on_a_green_ci_run_still_publishes_failure() -> None:
    """if a scope job skipped for an unknown reason reads as a designed skip then an admission bug hides on every head"""
    payload = publish_payload(
        replace(PUBLISH, scope_result="skipped", run_conclusion="success", scope_event="", in_scope="")
    )
    assert payload is not None and payload["conclusion"] == "failure"


def test_a_failed_scope_job_publishes_failure_not_skipped() -> None:
    """if a crashed scope job reads as a designed skip then an admission bug hides on every head"""
    payload = publish_payload(replace(PUBLISH, scope_result="failure", scope_event="", in_scope=""))
    assert payload is not None and payload["conclusion"] == "failure"
    assert "e2e scope job failure" in payload["output"]["title"]


def test_a_non_pull_request_run_publishes_nothing() -> None:
    """if main's push completion publishes on main's sha then main carries two e2e verdicts per merge"""
    assert publish_payload(replace(PUBLISH, run_event="push", pr_number="")) is None


@pytest.mark.parametrize("outcome", ["cancelled", "skipped", ""])
def test_an_outcome_that_is_not_a_conclusion_goes_red(outcome: str) -> None:
    """if a cancelled aggregate is published as a verdict then a superseded run writes a false conclusion"""
    with pytest.raises(AdmissionError, match="not a conclusion"):
        publish_payload(replace(PUBLISH, verdict_outcome=outcome))


def test_a_pull_request_run_without_a_head_sha_goes_red() -> None:
    """if an empty head sha is published then the Checks API answers 422 and the reason is lost"""
    with pytest.raises(AdmissionError, match="named no head sha"):
        publish_payload(replace(PUBLISH, head_sha=""))


# ----- the CLI ---------------------------------------------------------------------


def test_resolve_writes_every_output_and_a_notice_on_skip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """if resolve writes fewer outputs than the workflow reads then a later job reads an empty ref as main"""
    output = tmp_path / "out"
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_OUTPUT": str(output),
        "EVENT_NAME": "workflow_run",
        "RUN_EVENT": "pull_request",
        "RUN_URL": PR_RUN.run_url,
        "HEAD_SHA": HEAD,
        "PR_NUMBER": "6474",
        "PR_BASE": "main",
    }
    api = RecordedApi({GATE_PATH: {"check_runs": [_gate("completed", "failure")]}})
    assert main(["resolve"], env=env, api=api, sleep=_no_sleep) == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert [line.split("=", 1)[0] for line in lines] == ["scope_event", "checkout_ref", "repair", "pr", "skip"]
    assert "::notice title=E2E out of scope::ci gate at" in capsys.readouterr().out


def test_publish_posts_the_payload_to_the_check_runs_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    """if publish posts to the wrong path then the verdict never reaches the head"""
    posted: list[tuple[str, dict]] = []

    def api_post(path: str, body: dict) -> dict:
        posted.append((path, body))
        return {"html_url": "https://github.com/x/y/runs/1"}

    env = {
        "GITHUB_REPOSITORY": REPO,
        "RUN_EVENT": "pull_request",
        "HEAD_SHA": HEAD,
        "PR_NUMBER": "6474",
        "SCOPE_RESULT": "success",
        "SCOPE_EVENT": "pull_request",
        "SKIP": "",
        "IN_SCOPE": "true",
        "VERDICT_OUTCOME": "success",
        "RUN_URL": PUBLISH.run_url,
        "RUN_ID": PUBLISH.run_id,
    }
    assert main(["publish"], env=env, api_post=api_post) == 0
    assert posted == [(f"repos/{REPO}/check-runs", publish_payload(PUBLISH))]
    assert "e2e verdict success on" in capsys.readouterr().out


def test_an_admission_error_is_an_error_annotation_and_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a missing env var exits 0 then the workflow checks out main for a pull request"""
    env = {"GITHUB_REPOSITORY": REPO, "GITHUB_OUTPUT": str(tmp_path / "out")}
    assert main(["resolve"], env=env, api=RecordedApi(), sleep=_no_sleep) == 1
    assert "::error title=E2E admission::EVENT_NAME must be set" in capsys.readouterr().err


def test_write_outputs_refuses_a_multiline_value(tmp_path: Path) -> None:
    """if a multiline skip reason is written then GITHUB_OUTPUT parses the second line as a new key"""
    with pytest.raises(AdmissionError, match="newline"):
        write_outputs({"skip": "two\nlines"}, tmp_path / "out")
