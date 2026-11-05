"""Re-run only the failed pytest ids of a pull request head, and re-decide `ci gate` there.

pytest's `--last-failed`, at CI level and author-triggered (like Bazel's and Gradle's flaky
retry, but pinned to one head): a head whose `ci gate` went red on a few test ids gets those ids,
and nothing else, run again on the pytest pool, instead of five shards and every side workflow
re-running to prove a one-line fix. `.github/workflows/ci-rerun-failed.yml` drives it; it is
triggered by the `ci:rerun-failed` label or a `workflow_dispatch` with the PR number.

Stdlib only (like `scripts.ci_required_gate`), so the plan runs on the runner's own `python3`
before any venv is provisioned, and a refused rerun costs one checkout.

Requirements (CI-RERUN-8758, -8758b and -8758c, .planning/REQUIREMENTS.md):
  ✔︎ ✅ 🎯 R1 the rerun is pinned to the PR's CURRENT head and runs only that head's failed pytest ids
      [if] the head's latest ci.yml run is not completed, or its `ci gate` is not red [then ⛔️] refuse
      [if] the head moved between the trigger and the plan, or before the verdict [then ⛔️] refuse,
           the normal run on the new head applies
      [if] `ci gate` failed for anything but `FAIL test: failure` [then ⛔️] refuse quoting the line
      [if] a failed shard has no pytest id, exited other than 1, timed out or was cancelled
           [then ⛔️] refuse naming the shard (setup, timeout and runner kills are not test ids)
  ✔︎ ✅ 🎯 R2 caps: at most MAX_IDS ids, MAX_RERUNS_PER_HEAD reruns, never for inherited or repair reds
      [if] more than 200 ids failed [then ⛔️] refuse
      [if] two reruns already reported at this head [then ⛔️] refuse with a PR comment
      [if] any failed id also failed on main's tip run [then ⛔️] refuse, naming it inherited
      [if] the PR carries `ci:trunk-repair` [then ⛔️] refuse
  ✔︎ ✅ 🎯 R3 the verdict re-posts `ci gate` at the head, SUCCESS only when every failed id passed
      [if] any id failed again, or the JUnit report lacks one of them [then ⛔️] FAILURE naming it
      [if] pytest exited anything but 0 [then ⛔️] FAILURE, even when every listed id passed
      [if] every id passed and the rerun covered every one [then ⛔️] `ci gate` SUCCESS and a
           `ci gate (rerun-failed)` check with the one-line verdict, both on the pinned head

Regression lines live in (private path).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scripts.ci_failure_ids import failed_identities


@dataclass(frozen=True)
class CFG:
    LABEL: str = "ci:rerun-failed"
    REPAIR_LABEL: str = "ci:trunk-repair"
    MAX_IDS: int = 200
    MAX_RERUNS_PER_HEAD: int = 2
    CI_WORKFLOW: str = "ci.yml"
    #: The check the lander and branch protection read: the newest `ci gate` at the head.
    GATE_CHECK: str = "ci gate"
    #: One per rerun that reached a verdict; counting these at the head enforces the cap.
    RERUN_CHECK: str = "ci gate (rerun-failed)"
    #: The ci.yml `ci gate` FAIL line that a rerun of failed ids can answer, and no other.
    RERUNNABLE_GATE_FAIL: str = "FAIL test: failure"
    #: `pytest fast lane (shard N of 5)`: the ci.yml `test` job, the gate's one rerunnable need.
    SHARD_JOB: re.Pattern[str] = re.compile(r"^pytest fast lane \(shard \d+ of \d+\)$")
    #: The R2 selection record name, keyed under the SOURCE ci.yml run id so
    #: scripts/ci_selection_escapes.py finds it beside that run's own records (DEVOPS-20).
    RECORD_PREFIX: str = "pr-test-selection-"
    RECORD_SCHEMA: str = "rerun-failed/1"


_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z ?")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
#: The fast lane verdict step's annotation (`printf '::error title=...::pytest exited %s'`).
_PYTEST_EXIT = re.compile(r"^##\[error\]pytest exited ([0-9]+)$")
#: Runner lines no test failure produces: the job did not end on its tests alone.
_NOT_A_TEST_FAILURE = re.compile(
    r"The operation was canceled|exceeded the maximum execution time|No space left on device"
)
_PYTEST_ID = re.compile(r"^(?:FAILED|ERROR) (tests/\S.*)$")


# ----- types ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What a rerun will run, or why it will not run at all."""

    ok: bool
    reason: str
    pr: int
    head_sha: str
    source_run: int | None = None
    source_attempt: int | None = None
    ids_by_job: dict[str, list[str]] = field(default_factory=dict)
    rerun_index: int = 0

    @property
    def ids(self) -> list[str]:
        return sorted({node for ids in self.ids_by_job.values() for node in ids})


@dataclass(frozen=True)
class GateVerdict:
    ok: bool
    line: str
    details: tuple[str, ...]


# ----- log reading (pure) -----------------------------------------------------------------------


def _clean(line: str) -> str:
    parts = line.split("\t", 2)
    line = parts[2] if len(parts) == 3 else line
    return _ANSI.sub("", _TIMESTAMP.sub("", line, count=1)).rstrip("\r")


def gate_fail_lines(gate_log: str) -> list[str]:
    """The `FAIL ...` lines `scripts.ci_required_gate verdict` printed (not their ::error echo)."""
    return [line for line in map(_clean, gate_log.split("\n")) if line.startswith("FAIL ")]


def pytest_exit_code(shard_log: str) -> int | None:
    """The pytest exit code the fast lane verdict step reported, None when it never reported one."""
    codes = [int(m.group(1)) for line in shard_log.split("\n") if (m := _PYTEST_EXIT.match(_clean(line)))]
    return codes[-1] if codes else None


def split_identities(identities: frozenset[str]) -> tuple[list[str], list[str]]:
    """(pytest node ids, every identity that is not a pytest id) from `failed_identities`."""
    nodes, others = [], []
    for identity in sorted(identities):
        if match := _PYTEST_ID.match(identity):
            nodes.append(match.group(1))
        else:
            others.append(identity)
    return nodes, others


def shard_rerun_ids(job: Mapping[str, object], log: str) -> tuple[list[str], str | None]:
    """A failed shard's pytest ids, or the reason it cannot be answered by rerunning ids."""
    name = job["name"]
    if job.get("conclusion") != "failure":
        return [], f"{name} concluded {job.get('conclusion')}, not failure"
    if beyond := sorted({m.group(0) for line in log.split("\n") if (m := _NOT_A_TEST_FAILURE.search(line))}):
        return [], f"{name} failed beyond its tests: {beyond[0]}"
    rc = pytest_exit_code(log)
    if rc != 1:
        return [], f"{name} pytest exited {rc if rc is not None else 'never (no verdict step line)'}, not 1"
    nodes, others = split_identities(failed_identities(log))
    if others:
        return [], f"{name} names non-pytest failures: {', '.join(others[:3])}"
    if not nodes:
        return [], f"{name} exited 1 but names no failing test id"
    return nodes, None


# ----- planning (pure) --------------------------------------------------------------------------


def _pr_refusal(pr_state: Mapping[str, object], pinned_head: str, prior_reruns: int) -> str | None:
    if CFG.REPAIR_LABEL in set(pr_state.get("labels") or ()):  # type: ignore[arg-type]
        return f"the PR carries {CFG.REPAIR_LABEL}; a main repair takes the normal run"
    if pr_state.get("head_sha") != pinned_head:
        return f"the head moved to {str(pr_state.get('head_sha'))[:12]}; the normal run on it applies"
    if prior_reruns >= CFG.MAX_RERUNS_PER_HEAD:
        return f"{prior_reruns} reruns already reported at this head (cap {CFG.MAX_RERUNS_PER_HEAD})"
    return None


def _run_refusal(run: Mapping[str, object] | None, pinned_head: str) -> str | None:
    if run is None:
        return f"no {CFG.CI_WORKFLOW} pull_request run at this head yet"
    if run.get("head_sha") != pinned_head:
        return "the latest ci.yml run is for another head"
    if run.get("status") != "completed":
        return f"ci.yml run {run['id']} is {run.get('status')}; wait for it to finish"
    return None


def _gate_refusal(jobs: Sequence[Mapping[str, object]], logs: Mapping[int, str]) -> str | None:
    """None when the run's `ci gate` failed on the test job and nothing else."""
    gates = [job for job in jobs if job["name"] == CFG.GATE_CHECK]
    if len(gates) != 1:
        return f"the run has {len(gates)} `{CFG.GATE_CHECK}` jobs, expected 1"
    conclusion = gates[0].get("conclusion")
    if conclusion == "success":
        return f"`{CFG.GATE_CHECK}` is already green at this head; nothing to rerun"
    if conclusion != "failure":
        return f"`{CFG.GATE_CHECK}` concluded {conclusion}; only a failure is rerunnable"
    fails = gate_fail_lines(logs.get(int(gates[0]["id"]), ""))  # type: ignore[call-overload]
    if not fails:
        return f"`{CFG.GATE_CHECK}` failed but its log names no FAIL line (unmeasured)"
    if other := [line for line in fails if not line.startswith(CFG.RERUNNABLE_GATE_FAIL)]:
        return f"`{CFG.GATE_CHECK}` failed for more than tests: {other[0]}"
    return None


def _failed_shard_ids(
    jobs: Sequence[Mapping[str, object]], logs: Mapping[int, str]
) -> tuple[dict[str, list[str]], str | None]:
    shards = [job for job in jobs if CFG.SHARD_JOB.match(str(job["name"]))]
    failed = [job for job in shards if job.get("conclusion") not in ("success", "skipped")]
    if not failed:
        return {}, "`ci gate` says the test job failed but no shard job failed (unmeasured)"
    ids_by_job: dict[str, list[str]] = {}
    for job in failed:
        ids, why = shard_rerun_ids(job, logs.get(int(job["id"]), ""))  # type: ignore[call-overload]
        if why:
            return {}, why
        ids_by_job[str(job["name"])] = ids
    return ids_by_job, None


def plan_rerun(
    *,
    pr: int,
    pinned_head: str,
    pr_state: Mapping[str, object],
    run: Mapping[str, object] | None,
    jobs: Sequence[Mapping[str, object]],
    logs: Mapping[int, str],
    main_red: frozenset[str],
    prior_reruns: int,
) -> Plan:
    """Decide whether the head's latest ci.yml run can be answered by rerunning its failed ids.

    `jobs` is the run's `filter=latest` job list (a leg that passed in an earlier attempt shows
    that attempt's success); `logs` holds the `ci gate` job's log and every failed shard's.
    """
    source = (int(run["id"]), int(run["run_attempt"])) if run else (None, None)  # type: ignore[call-overload]
    refusal = _pr_refusal(pr_state, pinned_head, prior_reruns) or _run_refusal(run, pinned_head)
    refusal = refusal or _gate_refusal(jobs, logs)
    ids_by_job: dict[str, list[str]] = {}
    if refusal is None:
        ids_by_job, refusal = _failed_shard_ids(jobs, logs)
    plan = Plan(refusal is None, refusal or "", pr, pinned_head, *source, ids_by_job, prior_reruns + 1)
    if not plan.ok:
        return Plan(False, plan.reason, pr, pinned_head, *source)
    if len(plan.ids) > CFG.MAX_IDS:
        return Plan(False, f"{len(plan.ids)} failed ids exceed the {CFG.MAX_IDS}-id cap", pr, pinned_head, *source)
    if inherited := sorted(set(plan.ids) & main_red):
        reason = f"inherited from main's tip run, fix main first: {', '.join(inherited[:5])}"
        return Plan(False, reason, pr, pinned_head, *source)
    return plan


def main_red_ids(jobs: Sequence[Mapping[str, object]], logs: Mapping[int, str]) -> frozenset[str]:
    """The pytest ids main's tip run failed on, across its failed jobs."""
    red: set[str] = set()
    for job in jobs:
        if job.get("conclusion") == "failure":
            red.update(split_identities(failed_identities(logs.get(int(job["id"]), "")))[0])
    return frozenset(red)


# ----- verdict (pure) ---------------------------------------------------------------------------


def junit_key(node_id: str) -> tuple[str, str]:
    """pytest's JUnit (classname, name) for a node id: `tests/a/b.py::C::t[x]` is (`tests.a.b.C`, `t[x]`)."""
    path, _, rest = node_id.partition("::")
    module = path.removesuffix(".py").replace("/", ".")
    parts = rest.split("::") if rest else []
    if not parts:
        return module, ""
    return ".".join([module, *parts[:-1]]), parts[-1]


def junit_outcomes(xml_text: str) -> dict[tuple[str, str], str]:
    """(classname, name) to passed, skipped or failed, from a pytest JUnit report."""
    # The report is the one this job's own pytest just wrote to its private temp dir.
    root = ET.fromstring(xml_text)  # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    outcomes: dict[tuple[str, str], str] = {}
    for case in root.iter("testcase"):
        key = (case.get("classname") or "", case.get("name") or "")
        kinds = {child.tag for child in case}
        outcome = "failed" if kinds & {"failure", "error"} else "skipped" if "skipped" in kinds else "passed"
        if outcomes.get(key) != "failed":
            outcomes[key] = outcome
    return outcomes


def _outcome_of(node_id: str, outcomes: Mapping[tuple[str, str], str]) -> str:
    classname, name = junit_key(node_id)
    if name:
        return outcomes.get((classname, name), "missing")
    # A module id (a collection ERROR): covered when the module ran at least one case, none failed.
    found = [o for (cls, _), o in outcomes.items() if cls == classname or cls.startswith(classname + ".")]
    if not found:
        return "missing"
    return "failed" if "failed" in found else "passed"


def evaluate_gate(plan: Plan, outcomes: Mapping[tuple[str, str], str], pytest_rc: int) -> GateVerdict:
    """`ci gate` at the head after the rerun: SUCCESS only when every failed id now passes."""
    by_id = {node: _outcome_of(node, outcomes) for node in plan.ids}
    failed = sorted(node for node, o in by_id.items() if o == "failed")
    missing = sorted(node for node, o in by_id.items() if o == "missing")
    skipped = sorted(node for node, o in by_id.items() if o == "skipped")
    passed = len(by_id) - len(failed) - len(missing)
    ok = pytest_rc == 0 and not failed and not missing
    head = (
        f"[ci-gate] verdict: {'SUCCESS' if ok else 'FAILURE'} (rerun-failed {plan.rerun_index} of "
        f"{CFG.MAX_RERUNS_PER_HEAD}, ci.yml run {plan.source_run} attempt {plan.source_attempt}): "
        f"passed={passed}/{len(by_id)} failed={len(failed)} missing={len(missing)} pytest_rc={pytest_rc}"
    )
    details = [f"{'ok  ' if o in ('passed', 'skipped') else 'FAIL'} {node}: {o}" for node, o in sorted(by_id.items())]
    if pytest_rc != 0 and not failed and not missing:
        details.append(f"FAIL pytest exited {pytest_rc} although every listed id passed")
    if skipped:
        details.append(f"note: {len(skipped)} id(s) now skip; the lane counts a skip as not failing")
    return GateVerdict(ok, head, tuple(details))


def selection_record(plan: Plan, verdict: GateVerdict, rerun_run: str) -> dict[str, object]:
    """The DEVOPS-20 record of a rerun-failed landing, counted apart from the normal selection."""
    return {
        "schema": CFG.RECORD_SCHEMA,
        "mode": "rerun-failed",
        "pr_number": plan.pr,
        "head_sha": plan.head_sha,
        "source_run": plan.source_run,
        "source_attempt": plan.source_attempt,
        "rerun_run": rerun_run,
        "rerun_index": plan.rerun_index,
        "selected_ids": plan.ids,
        "verdict": "SUCCESS" if verdict.ok else "FAILURE",
        "verdict_line": verdict.line,
    }


def record_name(head_sha: str, rerun_index: int) -> str:
    return f"{CFG.RECORD_PREFIX}{head_sha}-rerun-failed-{rerun_index}"


# ----- GitHub (gh api) --------------------------------------------------------------------------


class Gh:
    """`gh api` on GH_TOKEN, nothing else. A failed call raises; nothing is read as empty."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _api(self, path: str, *flags: str) -> bytes:
        return subprocess.run(["gh", "api", *flags, path], capture_output=True, check=True).stdout

    def json(self, path: str) -> object:
        return json.loads(self._api(f"repos/{self.repo}/{path}"))

    def log(self, job_id: int) -> str:
        raw = self._api(f"repos/{self.repo}/actions/jobs/{job_id}/logs", "--allow-escape-sequences")
        return raw.decode("utf-8", errors="replace")

    def post(self, path: str, body: Mapping[str, object]) -> None:
        subprocess.run(
            ["gh", "api", "-X", "POST", f"repos/{self.repo}/{path}", "--input", "-"],
            input=json.dumps(body).encode(),
            capture_output=True,
            check=True,
        )

    def remove_label(self, pr: int, label: str) -> None:
        path = f"repos/{self.repo}/issues/{pr}/labels/{urllib.parse.quote(label, safe='')}"
        done = subprocess.run(["gh", "api", "-X", "DELETE", path], capture_output=True, check=False)
        if done.returncode != 0 and b"Label does not exist" not in done.stdout + done.stderr:
            raise subprocess.CalledProcessError(done.returncode, path, done.stdout, done.stderr)

    # -- reads --

    def pr_state(self, pr: int) -> dict[str, object]:
        data = self.json(f"pulls/{pr}")
        assert isinstance(data, dict)
        return {"head_sha": data["head"]["sha"], "labels": [label["name"] for label in data["labels"]]}

    def latest_run(self, head_sha: str, event: str, branch: str | None = None) -> dict | None:
        query = f"actions/workflows/{CFG.CI_WORKFLOW}/runs?event={event}&per_page=20"
        query += f"&branch={branch}&status=completed" if branch else f"&head_sha={head_sha}"
        runs = self.json(query)["workflow_runs"]  # type: ignore[index]
        return max(runs, key=lambda run: (run["created_at"], run["id"]), default=None)

    def latest_jobs(self, run_id: int) -> list[dict]:
        return self.json(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")["jobs"]  # type: ignore[index]

    def rerun_checks_at(self, head_sha: str) -> int:
        name = urllib.parse.quote(CFG.RERUN_CHECK)
        return int(self.json(f"commits/{head_sha}/check-runs?check_name={name}&filter=all")["total_count"])  # type: ignore[index]


# ----- CLI --------------------------------------------------------------------------------------


def _write_output(**values: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())


def _summary(lines: Sequence[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def cmd_plan(args: argparse.Namespace) -> int:
    gh = Gh(args.repo)
    pr_state = gh.pr_state(args.pr)
    pinned = args.head or str(pr_state["head_sha"])
    run = gh.latest_run(pinned, "pull_request")
    jobs = gh.latest_jobs(int(run["id"])) if run else []
    wanted = [j for j in jobs if j["name"] == CFG.GATE_CHECK or CFG.SHARD_JOB.match(j["name"])]
    logs = {int(j["id"]): gh.log(int(j["id"])) for j in wanted if j.get("conclusion") == "failure"}
    main_run = gh.latest_run("", "push", branch="main")
    if main_run is None:
        raise SystemExit("[rerun-failed] no completed ci.yml push run on main: main's red is unmeasured")
    main_jobs = [j for j in gh.latest_jobs(int(main_run["id"])) if j.get("conclusion") == "failure"]
    main_red = main_red_ids(main_jobs, {int(j["id"]): gh.log(int(j["id"])) for j in main_jobs})
    plan = plan_rerun(
        pr=args.pr,
        pinned_head=pinned,
        pr_state=pr_state,
        run=run,
        jobs=jobs,
        logs=logs,
        main_red=main_red,
        prior_reruns=gh.rerun_checks_at(pinned),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(plan), indent=2) + "\n", encoding="utf-8")
    (args.out.parent / "ids.txt").write_text("".join(f"{node}\n" for node in plan.ids), encoding="utf-8")
    word = "RUN" if plan.ok else "REFUSED"
    print(f"[rerun-failed] {word} PR #{plan.pr} head {pinned[:12]}: {plan.reason or f'{len(plan.ids)} id(s)'}")
    for job, ids in plan.ids_by_job.items():
        print(f"[rerun-failed]   {job}: {len(ids)} id(s)")
    _write_output(run=str(plan.ok).lower(), head_sha=pinned, ids=str(len(plan.ids)))
    _summary([f"rerun-failed {word}: {plan.reason or ', '.join(plan.ids[:20])}"])
    return 0


def _load_plan(path: Path) -> Plan:
    return Plan(**json.loads(path.read_text(encoding="utf-8")))


def cmd_verdict(args: argparse.Namespace) -> int:
    gh = Gh(args.repo)
    plan = _load_plan(args.plan)
    xml_text = args.junit.read_text(encoding="utf-8") if args.junit.is_file() else "<testsuites/>"
    verdict = evaluate_gate(plan, junit_outcomes(xml_text), args.pytest_rc)
    for line in (*verdict.details, verdict.line):
        print(line)
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(selection_record(plan, verdict, args.rerun_run), indent=2) + "\n")
    now = str(gh.pr_state(plan.pr)["head_sha"])
    if now != plan.head_sha:
        print(f"[rerun-failed] head moved to {now[:12]} during the rerun; no verdict posted, the normal run applies")
        _write_output(posted="false", verdict_line=verdict.line)
        return 0
    conclusion = "success" if verdict.ok else "failure"
    summary = "\n".join(["```", *verdict.details, "```", f"rerun run: {args.run_url}"])
    for name in (CFG.RERUN_CHECK, CFG.GATE_CHECK):
        gh.post(
            "check-runs",
            {
                "name": name,
                "head_sha": plan.head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "details_url": args.run_url,
                "output": {"title": verdict.line[:250], "summary": summary[:60000]},
            },
        )
    print(f"[rerun-failed] posted `{CFG.RERUN_CHECK}` and `{CFG.GATE_CHECK}` = {conclusion} on {plan.head_sha[:12]}")
    _write_output(posted="true", verdict_line=verdict.line)
    _summary([verdict.line, "", *verdict.details])
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    """Comment the outcome on the PR and take the label off, so adding it again triggers again."""
    gh = Gh(args.repo)
    plan = _load_plan(args.plan) if args.plan.is_file() else None
    if plan is None:
        body = f"rerun-failed: the plan never completed ({args.outcome}); see {args.run_url}"
    elif not plan.ok:
        body = f"rerun-failed refused: {plan.reason}. See {args.run_url}"
    else:
        body = f"rerun-failed: {args.verdict_line or f'no verdict ({args.outcome})'}\n\n{args.run_url}"
    gh.post(f"issues/{args.pr}/comments", {"body": body})
    gh.remove_label(args.pr, CFG.LABEL)
    print(f"[rerun-failed] commented on #{args.pr} and removed {CFG.LABEL}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), required=False)
    common.add_argument("--pr", type=int, required=True)
    p = sub.add_parser("plan", parents=[common])
    p.add_argument("--head", default="", help="pinned head SHA (the label event's); empty reads the PR's")
    p.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verdict", parents=[common])
    v.add_argument("--plan", type=Path, required=True)
    v.add_argument("--junit", type=Path, required=True)
    v.add_argument("--pytest-rc", type=int, required=True)
    v.add_argument("--record", type=Path, required=True)
    v.add_argument("--rerun-run", required=True)
    v.add_argument("--run-url", required=True)
    f = sub.add_parser("finish", parents=[common])
    f.add_argument("--plan", type=Path, required=True)
    f.add_argument("--outcome", default="")
    f.add_argument("--verdict-line", default="")
    f.add_argument("--run-url", required=True)
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    return {"plan": cmd_plan, "verdict": cmd_verdict, "finish": cmd_finish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
