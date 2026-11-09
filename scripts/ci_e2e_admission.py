"""E2E admission and verdict publication for a `workflow_run` of CI.

ADR-NEW-e2e-after-ci-gate (Wed 4 Nov 2026): e2e.yml runs a pull request's E2E as a
`workflow_run` of the CI workflow, so it starts only once `ci gate` is green. Two facts
about that event decide the shape of this module:

1. A workflow_run's own `github.sha` and `github.ref` are main's, and GitHub attaches
   every check-run it creates for the run's jobs to that sha, never to the pull request
   head (events-that-trigger-workflows, `workflow_run`: "the last commit on the default
   branch"). The head shows `e2e verdict` only because the verdict job PUBLISHES it, as
   a check-run through the Checks API, against `workflow_run.head_sha` (`publish`).
2. The payload names the pull request and the head CI ran, but `refs/pull/<n>/merge` is
   remade on every push, so a run queued behind a newer push would scope one revision
   and build or test another. `resolve` admits a run only while the pull request's head
   still equals the triggering head, and pins the merge commit sha once; every later
   job checks out that sha, never the moving ref.

A pull request's E2E is OPT-IN (ADR-NEW-e2e-opt-in-by-label, Mon 9 Nov 2026): when the
workflow passes `E2E_OPT_IN_LABEL`, `resolve` admits a green head only if the pull request
carries that label, and skips every other head with the reason, which `publish` turns into
a `skipped` verdict. An empty `E2E_OPT_IN_LABEL` (the workflow before that change) admits
every green head as before, so this module lands first and the workflow switches it on.

Both commands read the workflow's `env:` and write `$GITHUB_OUTPUT`; the GitHub API is
reached through `gh`, resolved by path (AGENTS.md, "Portable by default"). The decisions
are pure functions over the API payloads (`resolve_admission`, `publish_payload`), so
`tests/scripts/test_ci_e2e_admission.py` drives them with recorded responses.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Base branches a pull request's E2E runs for: main, and the batching branch whose
#: feature PRs land on main as one PR.
ADMITTED_BASES: frozenset[str] = frozenset({"main", "af--b2b-link"})
#: The ci.yml aggregator that admits a head.
GATE_CHECK_NAME = "ci gate"
#: The one check-run this workflow publishes on a pull request head. Pinned equal to
#: `scripts.ci_wait_workflows.WORKFLOW_RUN_PUBLISHED_CHECKS["e2e.yml"]` by the tests.
PUBLISHED_CHECK_NAME = "e2e verdict"
REPAIR_LABEL = "ci:trunk-repair"
#: The pull request label that opts a head in to E2E (ADR-NEW-e2e-opt-in-by-label). Pinned
#: equal to `scripts.ci_wait_workflows.WORKFLOW_RUN_OPT_IN_LABELS["e2e.yml"]` by the tests,
#: so ci-wait owes `e2e verdict` on exactly the heads admission can admit.
OPT_IN_LABEL = "e2e"
#: The workflow env that switches the opt-in on; empty or unset admits every green head.
OPT_IN_LABEL_ENV = "E2E_OPT_IN_LABEL"
#: GitHub computes a pull request's mergeability lazily after a push; the pulls endpoint
#: answers `mergeable: null` until it has, and asks the client to poll.
MERGEABILITY_ATTEMPTS = 6
MERGEABILITY_WAIT_S = 5.0

Api = Callable[[str], Any]


class AdmissionError(RuntimeError):
    """The run cannot be admitted or published and the job must go red, not skip."""


# ----- resolve: which revision, if any, this run tests ------------------------


@dataclass(frozen=True)
class RunEvent:
    """What the workflow knows about the triggering run before any API call."""

    repository: str
    event_name: str
    run_event: str
    run_url: str
    head_sha: str
    pr_number: str
    pr_base: str
    #: The label a pull request must carry to be admitted; "" admits every green head.
    opt_in_label: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RunEvent:
        return cls(
            repository=_required(env, "GITHUB_REPOSITORY"),
            event_name=_required(env, "EVENT_NAME"),
            run_event=env.get("RUN_EVENT", ""),
            run_url=env.get("RUN_URL", ""),
            head_sha=env.get("HEAD_SHA", ""),
            pr_number=env.get("PR_NUMBER", ""),
            pr_base=env.get("PR_BASE", ""),
            opt_in_label=env.get(OPT_IN_LABEL_ENV, ""),
        )


@dataclass(frozen=True)
class Admission:
    """The scope job's `pr` step outputs (names pinned by the workflow wiring tests)."""

    scope_event: str
    checkout_ref: str
    repair: bool
    pr: str
    skip: str

    def outputs(self) -> dict[str, str]:
        return {
            "scope_event": self.scope_event,
            "checkout_ref": self.checkout_ref,
            "repair": "true" if self.repair else "false",
            "pr": self.pr,
            "skip": self.skip,
        }


def gate_state(check_runs: list[dict]) -> str:
    """`"<status> <conclusion>"` of the latest `ci gate` check-run, `"absent absent"` with none."""
    if not check_runs:
        return "absent absent"
    latest = sorted(check_runs, key=lambda run: run.get("completed_at") or "")[-1]
    return f"{latest.get('status')} {latest.get('conclusion')}"


def resolve_admission(event: RunEvent, api: Api, sleep: Callable[[float], None] = time.sleep) -> Admission:
    """Decide what this run tests. A `workflow_run` of a pull request's green `ci gate`
    whose head is still the pull request's head is admitted at its pinned merge commit;
    every other case is a skip with its reason, and any other event is passed through
    (push, schedule and dispatch check out their own sha)."""
    if event.event_name != "workflow_run":
        return Admission(scope_event=event.event_name, checkout_ref="", repair=False, pr="", skip="")
    skip = _skip_before_pull(event, api)
    if skip:
        return Admission(scope_event="workflow_run", checkout_ref="", repair=False, pr=event.pr_number, skip=skip)
    pull = api(f"repos/{event.repository}/pulls/{event.pr_number}")
    skip = _skip_for_pull(event, pull, api, sleep)
    if skip:
        return Admission(scope_event="workflow_run", checkout_ref="", repair=False, pr=event.pr_number, skip=skip)
    merge_sha = pull.get("merge_commit_sha") or ""
    if not merge_sha:
        raise AdmissionError(f"pull request #{event.pr_number} is mergeable but names no merge_commit_sha")
    repair = _has_label(pull, REPAIR_LABEL)
    return Admission(scope_event="pull_request", checkout_ref=merge_sha, repair=repair, pr=event.pr_number, skip="")


def _has_label(pull: dict, name: str) -> bool:
    return any(label.get("name") == name for label in pull.get("labels") or [])


def _skip_before_pull(event: RunEvent, api: Api) -> str:
    if event.run_event != "pull_request":
        return f"CI run {event.run_url} was a {event.run_event} run, not a pull request; main's E2E is its own push run"
    if not event.pr_number:
        return f"CI run {event.run_url} names no pull request; nothing to check out"
    if event.pr_base not in ADMITTED_BASES:
        return f"pull request #{event.pr_number} targets {event.pr_base}, not {' or '.join(sorted(ADMITTED_BASES))}"
    check_name = GATE_CHECK_NAME.replace(" ", "%20")
    page = api(f"repos/{event.repository}/commits/{event.head_sha}/check-runs?check_name={check_name}&per_page=100")
    state = gate_state(page.get("check_runs") or [])
    if state != "completed success":
        return (
            f"{GATE_CHECK_NAME} at {event.head_sha[:10]} is '{state}', not green; "
            "E2E runs on the next CI completion whose gate is"
        )
    return ""


def _skip_for_pull(event: RunEvent, pull: dict, api: Api, sleep: Callable[[float], None]) -> str:
    if event.opt_in_label and not _has_label(pull, event.opt_in_label):
        # First, so an unlabelled head's verdict always says why, and no mergeability poll
        # (up to six reads) is spent on a head that cannot be admitted.
        return (
            f"pull request #{event.pr_number} carries no `{event.opt_in_label}` label; a pull request's E2E "
            "is opt-in (ADR-NEW-e2e-opt-in-by-label) and main's hourly E2E covers the merge"
        )
    head = (pull.get("head") or {}).get("sha") or ""
    if head != event.head_sha:
        return (
            f"pull request #{event.pr_number} head moved from {event.head_sha[:10]} to {head[:10]} "
            "after CI ran; the newer head's CI completion triggers its own E2E"
        )
    if pull["state"] != "open":
        # A closed pull request's `mergeable` stays null, so polling it can only go red. The lander
        # merges on a green ci gate, often before this run starts; main's push E2E covers the merge.
        how = "is already merged" if pull.get("merged") else "is closed"
        return f"pull request #{event.pr_number} {how}; there is no open merge commit to test"
    mergeable = pull.get("mergeable")
    attempts = 1
    while mergeable is None and attempts < MERGEABILITY_ATTEMPTS:
        sleep(MERGEABILITY_WAIT_S)
        pull.update(api(f"repos/{event.repository}/pulls/{event.pr_number}"))
        mergeable = pull.get("mergeable")
        attempts += 1
    if mergeable is None:
        raise AdmissionError(
            f"GitHub has not computed mergeability for pull request #{event.pr_number} after "
            f"{attempts} reads over {MERGEABILITY_WAIT_S * (attempts - 1):.0f} s; re-run this workflow"
        )
    if mergeable is False:
        return (
            f"pull request #{event.pr_number} is not mergeable into {event.pr_base}; there is no merge commit to test"
        )
    return ""


# ----- publish: the verdict on the pull request head --------------------------


@dataclass(frozen=True)
class PublishInputs:
    repository: str
    run_event: str
    head_sha: str
    pr_number: str
    scope_result: str
    #: The triggering CI run's conclusion (`workflow_run.conclusion`); empty before the
    #: workflow passes it. e2e.yml skips its scope job when this is not `success`.
    run_conclusion: str
    scope_event: str
    skip: str
    in_scope: str
    verdict_outcome: str
    run_url: str
    run_id: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PublishInputs:
        return cls(
            repository=_required(env, "GITHUB_REPOSITORY"),
            run_event=env.get("RUN_EVENT", ""),
            head_sha=env.get("HEAD_SHA", ""),
            pr_number=env.get("PR_NUMBER", ""),
            scope_result=_required(env, "SCOPE_RESULT"),
            run_conclusion=env.get("RUN_CONCLUSION", ""),
            scope_event=env.get("SCOPE_EVENT", ""),
            skip=env.get("SKIP", ""),
            in_scope=env.get("IN_SCOPE", ""),
            verdict_outcome=_required(env, "VERDICT_OUTCOME"),
            run_url=_required(env, "RUN_URL"),
            run_id=_required(env, "RUN_ID"),
        )


def publish_payload(inputs: PublishInputs) -> dict[str, Any] | None:
    """The Checks API body for `e2e verdict` on the pull request head, or None when the
    triggering CI run was not a pull request's (nothing to report to; main's E2E is its
    own push run, whose native check-run lands on main's sha)."""
    if inputs.run_event != "pull_request" or not inputs.pr_number:
        return None
    if not inputs.head_sha:
        raise AdmissionError("a pull request's CI run named no head sha to publish the verdict on")
    if inputs.scope_result == "skipped" and inputs.run_conclusion and inputs.run_conclusion != "success":
        # e2e.yml's scope job carries `if: workflow_run.conclusion == 'success'`: a CI run
        # that ended cancelled or red schedules no E2E job at all (two thirds of the
        # workflow_run runs on Mon 9 Nov 2026 were these). The head still gets its check.
        conclusion, title = "skipped", "E2E did not run at this head"
        summary = f"CI concluded {inputs.run_conclusion}; e2e.yml admits only a green ci gate: {inputs.run_url}"
    elif inputs.scope_result != "success":
        conclusion, title = "failure", f"e2e scope job {inputs.scope_result}: nothing was admitted"
        summary = f"The scope job did not resolve this run; see {inputs.run_url}."
    elif inputs.scope_event != "pull_request":
        conclusion, title = "skipped", "E2E did not run at this head"
        summary = inputs.skip or "The scope job admitted nothing and gave no reason."
    elif inputs.verdict_outcome == "success":
        conclusion = "success"
        title = "E2E green" if inputs.in_scope == "true" else "E2E out of scope for this change set"
        summary = f"Gate and scope aggregated green: {inputs.run_url}"
    elif inputs.verdict_outcome == "failure":
        conclusion, title = "failure", "E2E red"
        summary = f"The e2e gate or the scope decision failed: {inputs.run_url}"
    else:
        raise AdmissionError(f"verdict outcome {inputs.verdict_outcome!r} is not a conclusion to publish")
    return {
        "name": PUBLISHED_CHECK_NAME,
        "head_sha": inputs.head_sha,
        "status": "completed",
        "conclusion": conclusion,
        # ci-wait (scripts/ci_wait_core.py `_run_id`) reads the run id from here: the
        # html_url GitHub assigns an API-created check-run names no Actions run.
        "details_url": inputs.run_url,
        "external_id": inputs.run_id,
        "output": {"title": title, "summary": summary},
    }


# ----- plumbing ----------------------------------------------------------------


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise AdmissionError(f"{name} must be set in the step's env")
    return value


def _gh_path() -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise AdmissionError("gh is not on PATH; the runner image must provide it")
    return gh


def gh_api(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    """One `gh api` call, parsed. A non-zero exit raises with gh's own stderr."""
    argv = [_gh_path(), "api", "--method", method, path]
    stdin = None
    if body is not None:
        argv += ["--input", "-"]
        stdin = json.dumps(body)
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AdmissionError(f"gh api {method} {path} exited {proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def write_outputs(outputs: Mapping[str, str], output_path: Path) -> None:
    with output_path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in outputs.items():
            if "\n" in value:
                raise AdmissionError(f"output {key} holds a newline; one-line outputs only")
            handle.write(f"{key}={value}\n")
            print(f"[e2e-scope] {key}={value}")


def _cmd_resolve(env: Mapping[str, str], api: Api, sleep: Callable[[float], None]) -> int:
    admission = resolve_admission(RunEvent.from_env(env), api, sleep)
    write_outputs(admission.outputs(), Path(_required(env, "GITHUB_OUTPUT")))
    if admission.skip:
        print(f"::notice title=E2E out of scope::{admission.skip}")
    return 0


def _cmd_publish(env: Mapping[str, str], api_post: Callable[[str, dict[str, Any]], Any]) -> int:
    inputs = PublishInputs.from_env(env)
    payload = publish_payload(inputs)
    if payload is None:
        print(f"[e2e-publish] CI run was a {inputs.run_event or 'non-pull-request'} run; no head to publish on")
        return 0
    created = api_post(f"repos/{inputs.repository}/check-runs", payload)
    print(
        f"[e2e-publish] {payload['name']} {payload['conclusion']} on {inputs.head_sha[:10]} "
        f"(pull request #{inputs.pr_number}): {created.get('html_url', '<no html_url>')}"
    )
    return 0


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    api: Api | None = None,
    api_post: Callable[[str, dict[str, Any]], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("resolve", help="admit this run and pin its revision into $GITHUB_OUTPUT")
    commands.add_parser("publish", help="create the e2e verdict check-run on the pull request head")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.command == "resolve":
            return _cmd_resolve(env, api or gh_api, sleep)
        return _cmd_publish(env, api_post or (lambda path, body: gh_api(path, "POST", body)))
    except AdmissionError as error:
        print(f"::error title=E2E admission::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
