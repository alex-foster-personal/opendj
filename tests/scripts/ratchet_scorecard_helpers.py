"""Shared helpers for the test_ratchet_scorecard test modules (split from test_ratchet_scorecard.py)."""

from __future__ import annotations

import ast
import base64
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import ratchet_scorecard as rs

T0 = dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.UTC)
QUALITY_LOG = (
    "2026-10-15T09:01:02.1234567Z [quality] REGRESSION        "
    "complexity.blocks_over_limit: 253 > 250 allowed + 2 slack\n"
    "2026-10-15T09:01:02.1234567Z [quality] FAIL: 1 metric(s) got worse.\n"
)
PYTEST_LOG = (
    "2026-10-15T09:01:02.1234567Z FAILED (private path)::test_tip[a b] - assert 1\n"
    "2026-10-15T09:01:02.1234567Z FAILED tests/webui/test_x.py::test_y - boom\n"
)


def _head(
    branch: str,
    sha: str,
    hours: float,
    *red: str,
    measured: bool = True,
    cancelled: bool = False,
    e2e_required: bool = True,
) -> rs.Head:
    draft = branch.startswith("trunk-merge/")  # an authenticated draft unless a test says not
    at = T0 + dt.timedelta(hours=hours)
    return rs.Head(
        "ci.yml", branch, sha, at, frozenset(red), measured, draft=draft, cancelled=cancelled, e2e_required=e2e_required
    )


def _complete(heads: list[rs.Head]) -> list[rs.Head]:
    """Add the green e2e.yml verdict every PR sha also gets, where a test names none."""
    have = {(h.branch, h.sha) for h in heads if h.workflow == "e2e.yml"}
    e2e = [
        rs.Head("e2e.yml", h.branch, h.sha, h.at, frozenset(), True)
        for h in heads
        if h.workflow == "ci.yml" and (h.branch, h.sha) not in have
    ]
    return heads + e2e


AFFECTED_LOG = """\
2026-10-16T03:40:01.1000000Z PR TEST SELECTION: shard 2 of 5 runs mode=AFFECTED
2026-10-16T03:40:01.1000000Z   reason: 2 affected test module(s)
2026-10-16T03:40:01.1000000Z   selected 2 module(s) for 1 changed path(s); this shard runs 2
2026-10-16T03:40:01.1000000Z     (private path)
2026-10-16T03:40:01.1000000Z     tests/webui/
2026-10-16T03:40:09.1000000Z ============ 12 passed in 3.1s ============
"""


RELAND = "reland-6201"
# The passing shard of 00159db8c (run 38888830941, attempt 2) whose selection report decides
# whether it cleared bc6b59d35's pytest red.
RELAND_SELECTION_JOB = 116958163563


def _gate(cls: str, branches: int, sole: int = 0, **fixes: int) -> rs.GateScore:
    gate = rs.GateScore("k", cls, branches={str(i) for i in range(branches)}, sole_red_heads=sole)
    gate.fixes.update(fixes)
    return gate


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ratchet_scorecard"
RED, FIX, OWN = (
    "327e26688152b1b6fc359efab777adfc136fbe9a",
    "a8bd6a6166ed40d0c8cb078e8703f6bcc9dd4f54",
    "963fa0dc7f12e5860a6ce31be9de9352e35fee9f",
)
FIX_AT = "2026-10-15T21:23:44Z"  # FIX's committer date
MAIN = "4816464739ef78cc01d678f54999ec91704169dd"  # FIX's second parent: the main it merged


def _manifest() -> dict[str, str]:
    lines = (FIXTURES / "MANIFEST.sha256").read_text().splitlines()
    return {name: digest for digest, name in (line.split("  ", 1) for line in lines if line.strip())}


def _verified(name: str) -> str:
    """A fixture's text, only after its bytes match the committed manifest."""
    data = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == _manifest()[name], f"{name} differs from MANIFEST.sha256"
    return data.decode()


CMDK = "cmdk-v2-ai"
CMDK_RED, CMDK_FIX = "521027f88", "0afb8659e"


def _cmdk_parent(compare: dict, short: str) -> str:
    """The full sha of a commit, or of a parent, named by its 9-character prefix in the compare."""
    shas = {c["sha"] for c in compare["commits"]} | {p["sha"] for c in compare["commits"] for p in c["parents"]}
    return next(sha for sha in shas if sha.startswith(short))


def _cmdk_api(calls: list[str]):
    """Serve the captured af--cmdk-v2-ai responses (fixtures README) by request path. A runs
    listing returns the captured runs created inside the requested span, as GitHub does."""
    compare = json.loads(_verified(f"{CMDK}/compare-521027f88...0afb8659e.json"))
    full = {c["sha"][:9]: c["sha"] for c in compare["commits"]}
    full[CMDK_RED] = next(p["sha"] for c in compare["commits"] for p in c["parents"] if p["sha"].startswith(CMDK_RED))

    def api(path: str) -> str:
        calls.append(path)
        listing = re.match(
            r"repos/[^/]+/[^/]+/actions/workflows/(\w+)\.yml/runs\?.*&page=(\d+)&.*created=(\S+)\.\.(\S+)$", path
        )
        if listing:
            runs = json.loads(_verified(f"{CMDK}/runs-{listing.group(1)}.json"))["workflow_runs"]
            lo, hi = (rs._iso(x) for x in listing.group(3, 4))
            page = [r for r in runs if lo <= rs._iso(r["created_at"]) <= hi] if listing.group(2) == "1" else []
            return json.dumps({"workflow_runs": page})
        served = {
            **{
                f"repos/{rs.REPO}/actions/runs/{run}/attempts/1/jobs?per_page=100": f"jobs-{run}-1.json"
                for run in (38635253146, 38635253356, 38645499936, 38645499875)
            },
            f"repos/{rs.REPO}/actions/jobs/116096788066/logs": "log-116096788066.txt",
            f"repos/{rs.REPO}/compare/{full[CMDK_RED]}...{full[CMDK_FIX]}": "compare-521027f88...0afb8659e.json",
        }
        for name in (FIXTURES / "cmdk-v2-ai").iterdir():
            if commit := re.fullmatch(r"commit-(\w{9})\.json", name.name):
                served[f"repos/{rs.REPO}/commits/{full[commit.group(1)]}?per_page=100&page=1"] = name.name
            elif onmain := re.fullmatch(r"compare-(\w{9})\.\.\.main\.json", name.name):
                served[f"repos/{rs.REPO}/compare/{_cmdk_parent(compare, onmain.group(1))}...main?per_page=1"] = (
                    name.name
                )
            elif pair := re.fullmatch(r"compare-(\w{9})\.\.\.(\w{9})\.json", name.name):
                base, head = (_cmdk_parent(compare, x) for x in pair.groups())
                served.setdefault(f"repos/{rs.REPO}/compare/{base}...{head}", name.name)
        if path not in served:
            raise RuntimeError(f"no captured response for {path}")
        body = _verified(f"{CMDK}/{served[path]}")
        # A contents read answers base64 inside JSON, as the API does; every other capture is
        # the response body verbatim.
        return json.dumps({"content": base64.b64encode(body.encode()).decode()}) if "/contents/" in path else body

    return api


def _chain_hours(workflow: Path) -> float:
    """Longest time from a run's creation to its finish: 24 h of queue plus the timeout for
    each job on its deepest `needs` chain (GitHub's default timeout is 360 minutes)."""
    import yaml

    jobs = yaml.safe_load(workflow.read_text())["jobs"]

    def cost(name: str) -> int:
        needs = jobs[name].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        return 24 * 60 + jobs[name].get("timeout-minutes", 360) + max((cost(n) for n in needs), default=0)

    return max(cost(name) for name in jobs) / 60


def _replay(calls: list[str]):
    """Serve the captured GitHub responses (see the fixtures README) by request path."""
    served = {
        f"repos/{rs.REPO}/compare/{RED}...{FIX}": "compare-327e26688...a8bd6a616.json",
        f"repos/{rs.REPO}/compare/{FIX}...{RED}": "compare-a8bd6a616...327e26688.json",
        f"repos/{rs.REPO}/commits/{OWN}?per_page=100&page=1": "commit-963fa0dc7.json",
        f"repos/{rs.REPO}/commits/{FIX}?per_page=100&page=1": "commit-a8bd6a616.json",
        f"repos/{rs.REPO}/compare/{MAIN}...{FIX}": "compare-481646473...a8bd6a616.json",
        f"repos/{rs.REPO}/compare/{MAIN}...{OWN}": "compare-481646473...963fa0dc7.json",
        f"repos/{rs.REPO}/compare/{OWN}...{MAIN}": "compare-963fa0dc7...481646473.json",
        f"repos/{rs.REPO}/compare/{MAIN}...main?per_page=1": "compare-481646473...main.json",
        f"repos/{rs.REPO}/commits/{MAIN}/pulls?per_page=100": "pulls-481646473.json",
    }

    def api(path: str) -> str:
        calls.append(path)
        if path not in served:
            raise RuntimeError(f"no captured response for {path}")
        return _verified(served[path])

    return api


def _fake_gh(directory: Path, version: str) -> Path:
    """A stand-in `gh` that reports `version` for `--version` and otherwise echoes its argv plus
    one escape-bearing line, the shape a `--allow-escape-sequences` read returns."""
    fake = directory / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "gh version {version} (2026-09-24)"\n'
        "  exit 0\n"
        "fi\n"
        'printf "%s\\n" "$@"\n'
        "printf '\\033[31mred\\033[0m\\n'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _read_through_fake_gh(directory: Path) -> tuple[str, str]:
    """`_gh_api`'s returned body for one job-log read and one JSON read, run in a child whose
    PATH holds only `directory`."""
    code = (
        "from scripts import ratchet_scorecard as rs\n"
        "print(repr(rs._gh_api('repos/o/r/actions/jobs/7/logs')))\n"
        "print(repr(rs._gh_api('repos/o/r/runs?per_page=1')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={"PATH": str(directory)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    log_repr, json_repr = proc.stdout.splitlines()
    return ast.literal_eval(log_repr), ast.literal_eval(json_repr)


def _gh_api_child(directory: Path, expression: str) -> subprocess.CompletedProcess[str]:
    """Run one `_gh_api` expression in a child whose PATH holds only `directory`.

    A child, not a patched `shutil.which`: the version gate and the decoder are both real
    subprocess behavior, and the repo's rule is no mocked APIs or outputs.
    """
    return subprocess.run(
        [sys.executable, "-c", f"from scripts import ratchet_scorecard as rs\n{expression}\n"],
        cwd=REPO_ROOT,
        env={"PATH": str(directory)},
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_gh_bad_version(directory: Path, mode: str) -> Path:
    """A stand-in `gh` whose `--version` cannot settle the flag: `failing` exits non-zero,
    `unparsable` answers without a version. Either way an `api` call would echo its argv."""
    fake = directory / "gh"
    version = (
        'echo "error: no such option" >&2\n  exit 1\n' if mode == "failing" else 'echo "gh (unknown build)"\n  exit 0\n'
    )
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f"  {version}"
        "fi\n"
        'printf "%s\\n" "$@"\n'
        "printf '\\033[31mred\\033[0m\\n'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _fake_gh_bad_body(directory: Path) -> Path:
    """A stand-in `gh` that serves a job log whose bytes are not UTF-8: 0xFF is invalid in every
    position, and `errors="replace"` used to turn it into U+FFFD before the key was parsed."""
    fake = directory / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        '  echo "gh version 2.97.0 (2026-09-24)"\n'
        "  exit 0\n"
        "fi\n"
        'printf "%s\\n" "$@"\n'
        "printf 'FAILED tests/x.py::test_y - \\377\\376\\n'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


# The fake is a POSIX shell script, which Windows cannot launch: `shutil.which` finds no
# PATHEXT match and `CreateProcess` has no interpreter for `#!`. The behavior under test is
# `_gh_api`'s argv, not a platform trait, so the portable fake is a separate change.
_posix_fake_gh = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake `gh` is a POSIX #!/bin/sh script (no PATHEXT match)"
)


def _job(name: str, conclusion: str, job_id: int = 1) -> dict:
    return {"id": job_id, "name": name, "conclusion": conclusion, "steps": []}


RATCHET_MODULE = "(private path)"


def _scoping_api(selection_report: str, skip_quality: str | None = None) -> rs.Api:
    """Serve a four-branch PR fleet the way the Actions API serves one: three branches each
    clear a ratchet pytest red (`a` with code, `b` with tests, `c` with paperwork), and a
    fourth carries a measured head red on `quality:complexity.blocks_over_limit` whose own pytest shard's log
    reports `selection_report`. That shard's selection is consulted because branch `d` also
    carries a pytest red, which is the only way a passing shard's report is ever read."""
    red, fix = T0 + dt.timedelta(minutes=30), T0 + dt.timedelta(minutes=90)
    shard = "pytest fast lane (shard 1 of 5)"
    runs_step = "Run fast pytest lane shard (full suite + coverage live in Full CI, on-demand)"
    ran = {"name": runs_step, "conclusion": "success"}
    broke = {"name": runs_step, "conclusion": "failure"}
    quality_step = {"name": "Quality ratchet (report + ratchet)", "conclusion": "failure"}
    e2e_step = {"name": "Run e2e", "conclusion": "success"}
    fail_log = (
        f"2026-10-15T06:30:00.0000000Z PR TEST SELECTION: shard 1 of 5 runs mode=FULL\n"
        f"FAILED {RATCHET_MODULE}::test_x - boom\n"
    )
    report = "2026-10-15T06:30:00.0000000Z PR TEST SELECTION: shard 1 of 5 runs mode=FULL\n"

    def run(n: int, workflow: str, branch: str, sha: str, conclusion: str, at: dt.datetime) -> dict:
        stamp = f"{at:%Y-%m-%dT%H:%M:%SZ}"
        return {
            "id": n,
            "event": "pull_request",
            "head_branch": branch,
            "head_repository": {"full_name": rs.REPO},
            "head_sha": sha,
            "conclusion": conclusion,
            "run_attempt": 1,
            "created_at": stamp,
            "updated_at": stamp,
        }

    runs: dict[str, list[dict]] = {"ci.yml": [], "e2e.yml": []}
    jobs: dict[int, list[dict]] = {}
    logs: dict[int, str] = {}
    seq = [0, 1000]  # run ids, then job ids, so a job's log is keyed by the id GitHub gave it

    def publish(
        workflow: str, branch: str, sha: str, conclusion: str, at: dt.datetime, *specs: tuple[str, str, dict]
    ) -> list[dict]:
        """One run and its jobs; each spec is (job name, conclusion, the step it ran)."""
        seq[0] += 1
        runs[workflow].append(run(seq[0], workflow, branch, sha, conclusion, at))
        made = []
        for name, verdict, step in specs:
            seq[1] += 1
            made.append({"id": seq[1], "name": name, "conclusion": verdict, "steps": [step]})
        jobs[seq[0]] = made
        return made

    for branch in "abc":
        (shard_job,) = publish("ci.yml", branch, f"{branch}1", "failure", red, (shard, "failure", broke))
        logs[shard_job["id"]] = fail_log
        (clearing,) = publish("ci.yml", branch, f"{branch}2", "success", fix, (shard, "success", ran))
        logs[clearing["id"]] = report
        for sha, at in ((f"{branch}1", red), (f"{branch}2", fix)):
            publish("e2e.yml", branch, sha, "success", at, ("e2e gate", "success", e2e_step))
    # Branch `d`: d0 is red on the ratchet pytest, so d1's shard report is consulted; d1 is
    # red on the quality ratchet and its passing shard's log reports `selection_report`.
    (d0,) = publish("ci.yml", "d", "d0", "failure", red, (shard, "failure", broke))
    logs[d0["id"]] = fail_log
    publish("e2e.yml", "d", "d0", "success", red, ("e2e gate", "success", e2e_step))
    quality, passing = publish(
        "ci.yml", "d", "d1", "failure", fix, ("quality ratchet", "failure", quality_step), (shard, "success", ran)
    )
    logs[quality["id"]] = QUALITY_LOG
    logs[passing["id"]] = selection_report
    publish("e2e.yml", "d", "d1", "success", fix, ("e2e gate", "success", e2e_step))
    if skip_quality is not None:
        # Branch `e`: a measured head red on a frontend ratchet step whose quality job was
        # skipped and whose `ci gate` failed (`aggregate`) or passed (`docs-only`). No pytest
        # red on the branch, so no passing shard's selection report is read.
        bundles = {"name": "Bundle budgets (per-surface gzip ceilings)", "conclusion": "failure"}
        quality_step_skipped = {"name": "Quality ratchet (report + ratchet)", "conclusion": "skipped"}
        publish(
            "ci.yml",
            "e",
            "e0",
            "failure",
            red,
            ("frontend unit + check + build", "failure", bundles),
            (rs.QUALITY_JOB, "skipped", quality_step_skipped),
            (
                "ci gate",
                "failure" if skip_quality == "aggregate" else "success",
                {"name": "Evaluate", "conclusion": "success"},
            ),
        )
        publish("e2e.yml", "e", "e0", "success", red, ("e2e gate", "success", e2e_step))

    def api(path: str) -> str:
        listing = re.match(rf"repos/{rs.REPO}/actions/workflows/(\w+)\.yml/runs\?.*&created=(\S+)\.\.(\S+)$", path)
        if listing:
            lo, hi = (rs._iso(x) for x in listing.groups()[1:])
            page = [r for r in runs[f"{listing.group(1)}.yml"] if lo <= rs._iso(r["created_at"]) <= hi]
            return json.dumps({"workflow_runs": page})
        if job := re.fullmatch(rf"repos/{rs.REPO}/actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100", path):
            return json.dumps({"jobs": jobs[int(job.group(1))]})
        if log := re.fullmatch(rf"repos/{rs.REPO}/actions/jobs/(\d+)/logs", path):
            if int(log.group(1)) not in logs:
                raise RuntimeError("gh: HTTP 410")
            return logs[int(log.group(1))]
        raise RuntimeError(f"no response for {path}")

    return api


def _scoping_fix_files(red_sha: str, _fix: str) -> Sequence[str]:
    """One fix kind per clearing branch, so no single kind can decide the pytest gate alone."""
    return {"a1": ["scripts/ratchet_scorecard.py"], "b1": ["tests/webui/test_x.py"], "c1": [".planning/x.md"]}.get(
        red_sha, []
    )


MAIN_QUALITY = "main-quality"


def _actions_replay(
    fixture: str, calls: list[str] | None = None, lost: frozenset[int] | set[int] = frozenset()
) -> rs.Api:
    """Serve one captured fixture set the way the Actions API served it: a workflow's run
    listing filtered to the requested created span (one page), each run attempt's jobs, and
    each job's log. Job ids in `lost` answer 410, as an expired log does; anything not
    captured raises."""

    def api(path: str) -> str:
        if calls is not None:
            calls.append(path)
        if listing := re.match(r"repos/[^/]+/[^/]+/actions/workflows/(\w+)\.yml/runs\?.*&page=(\d+)&", path):
            name = f"{fixture}/runs-{listing.group(1)}.json"
            runs = json.loads(_verified(name))["workflow_runs"] if (FIXTURES / name).exists() else []
            span = re.search(r"created=(\S+)\.\.(\S+)$", path)
            assert span is not None, path
            lo, hi = (rs._iso(x) for x in span.groups())
            runs = [r for r in runs if lo <= rs._iso(r["created_at"]) <= hi]
            return json.dumps({"workflow_runs": runs if listing.group(2) == "1" else []})
        if job := re.fullmatch(rf"repos/{rs.REPO}/actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100", path):
            return _verified(f"{fixture}/jobs-{job.group(1)}-{job.group(2)}.json")
        if log := re.fullmatch(rf"repos/{rs.REPO}/actions/jobs/(\d+)/logs", path):
            if int(log.group(1)) in lost:
                raise RuntimeError("gh: HTTP 410")
            return _verified(f"{fixture}/log-{log.group(1)}.txt")
        raise RuntimeError(f"no captured response for {path}")

    return api


def _main_api(calls: list[str]) -> rs.Api:
    """Real responses for one main push (fa5707891, Fri 16 Oct 2026): ci.yml run 38894149191,
    whose report-only quality job logged `mypy.errors_tests: 466 > 460 allowed + 5 slack`, and
    the passing e2e.yml run 38894149533 beside it."""
    return _actions_replay(MAIN_QUALITY, calls)
