"""Every CI job that runs analysis warms the numba JIT cache first (#1316).

The failure this pins is not a flaky test. librosa's
``@numba.guvectorize(..., cache=True)`` hot paths compile on first call into
``.nbi``/``.nbc`` artifacts in one shared cache. Two processes meeting a COLD
cache interleave those writes, and from then on every process that LOADS the
result dies at a NULL instruction pointer - exit 139, no traceback, no Python
exception. On the self-hosted runners the workspace and its `.venv` persist
between runs, so one poisoned cache killed every later e2e job on that runner
until the artifacts were deleted by hand (five workspaces, Sat 5 Sep 2026).

So the e2e gate needs BOTH halves and in this order: PURGE, because warming
cannot repair a cache that is already corrupt (loading the corrupt entry is
what segfaults, so no in-process code runs after it), and WARM, because a
purge alone leaves the cache cold, which is the state that corrupts.

These assertions are about the workflow files, so they cost nothing and run on
every PR. They are deliberately not "does the yaml mention numba": each one
names a specific way the wiring can be silently wrong.

Regression lines:
  - if the e2e gate drops the JIT cache step then a poisoned cache outlives
    the job again and every later run on that runner segfaults
  - if that step loses --purge then an already-corrupt cache is never repaired
  - if the step moves after the e2e run then it warms a cache the run already
    raced into
  - if a pytest job drops its warm-up then the one-off ~35 s compile lands
    inside whichever test touches analysis first and reads as a timeout
  - if any job reimplements the warm-up inline instead of invoking
    apps.analysis.jit_warmup then CI can drift from the warm-up that ships
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The module every CI warm-up must invoke. Named rather than reimplemented,
#: so a CI step cannot drift from the warm-up `apps.analysis.run` really runs.
WARMUP_MODULE = "apps.analysis.jit_warmup"

#: Jobs that start `apps.analysis.run` processes, or a suite that does.
#: (workflow, job id, purge required)
JITS = (
    # The gate spawns concurrent analysis processes from the engine's refresh
    # route, on a runner whose workspace and .venv survive between jobs. This
    # is the job whose caches were found poisoned on disk.
    ("e2e.yml", "gate", True),
    # The pytest lanes run single-process and have never produced the state,
    # so they warm without purging: a purge would buy a cold ~35 s compile on
    # every shard of every PR to insure against a state nothing here creates.
    ("ci.yml", "test", False),
    ("full-ci.yml", "test", False),
)


def _job(workflow: str, job_id: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return doc["jobs"][job_id]


def _warmup_step_index(job: dict) -> int | None:
    for i, step in enumerate(job["steps"]):
        if WARMUP_MODULE in (step.get("run") or ""):
            return i
    return None


@pytest.mark.requirement("JIT-05")
@pytest.mark.parametrize(("workflow", "job_id", "needs_purge"), JITS)
def test_job_warms_the_jit_cache_by_invoking_the_shipped_module(
    workflow: str, job_id: str, needs_purge: bool
) -> None:
    """[if] a CI job stops invoking the shipped warm-up module [then] fail, [else stop].

    if a job stops invoking apps.analysis.jit_warmup then its warm-up is
    either gone or is CI's own copy, free to drift from the shipped one"""
    job = _job(workflow, job_id)
    index = _warmup_step_index(job)
    assert index is not None, (
        f"{workflow}:{job_id} has no step invoking {WARMUP_MODULE} (#1316)"
    )
    run = job["steps"][index]["run"]
    assert "--backend librosa" in run, (
        f"{workflow}:{job_id} warms an unnamed backend: {run!r}"
    )
    assert ("--purge" in run) is needs_purge, (
        f"{workflow}:{job_id} purge is {'--purge' in run}, expected {needs_purge}; "
        "see the comment on JITS for why each lane differs"
    )


@pytest.mark.requirement("JIT-05")
@pytest.mark.parametrize(("workflow", "job_id", "needs_purge"), JITS)
def test_the_warmup_runs_before_anything_that_analyzes(
    workflow: str, job_id: str, needs_purge: bool
) -> None:
    """[if] a job warms the cache after the thing that analyzes [then] fail, [else stop].

    if the warm-up lands after the suite or the e2e run then it warms a
    cache those processes have already raced into, which is no warm-up at all"""
    job = _job(workflow, job_id)
    index = _warmup_step_index(job)
    assert index is not None, f"{workflow}:{job_id} has no {WARMUP_MODULE} step"

    consumers = [
        i
        for i, step in enumerate(job["steps"])
        for run in [step.get("run") or ""]
        if "pytest" in run or "playwright" in run or "pnpm test" in run
    ]
    assert consumers, (
        f"fixture precondition: {workflow}:{job_id} runs nothing that analyzes, "
        "so an ordering assertion here would pass vacuously"
    )
    assert index < min(consumers), (
        f"{workflow}:{job_id} warms the JIT cache at step {index}, after the "
        f"first consumer at step {min(consumers)}"
    )


@pytest.mark.requirement("JIT-05")
def test_the_e2e_gate_purges_before_it_warms() -> None:
    """[if] the e2e gate stops purging, or splits purge and warm apart [then] fail, [else stop].

    if warm came before purge then the purge would delete what was just
    compiled and leave the job running on a cold cache - the corrupting state"""
    job = _job("e2e.yml", "gate")
    index = _warmup_step_index(job)
    assert index is not None
    run = job["steps"][index]["run"]
    assert "--purge" in run, run
    # One command, so the order is the CLI's, not the workflow's. Pinned here
    # because splitting it into two steps is the plausible refactor that would
    # reintroduce the ordering bug.
    assert run.count(WARMUP_MODULE) == 1, (
        "the gate's purge and warm must stay ONE invocation, so their order "
        f"cannot be got wrong by editing yaml: {run!r}"
    )


@pytest.mark.requirement("JIT-05")
def test_no_workflow_reimplements_the_purge_inline() -> None:
    """[if] a workflow deletes cache artifacts by hand [then] fail, [else stop].

    if a workflow deletes .nbi/.nbc with its own find/rm then the purge and
    the fingerprint stop agreeing about what an artifact is"""
    offenders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if (".nbi" in stripped or ".nbc" in stripped) and (
                "rm " in stripped or "-delete" in stripped or "unlink" in stripped
            ):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, (
        "these workflow lines purge the JIT cache by hand instead of calling "
        f"{WARMUP_MODULE} --purge:\n" + "\n".join(offenders)
    )


@pytest.mark.requirement("JIT-05")
def test_some_per_pr_job_runs_the_jit_guards_the_sharded_lane_ignores() -> None:
    """[if] no per-PR job runs the JIT guards the sharded lane ignores [then] fail, [else stop].

    The sharded `test` job carries `--ignore=tests/analysis`, which would
    silently take the lock, ordering and fingerprint guards out of every PR and
    leave them only in on-demand Full CI - the same as not gating them. It
    cannot host the scoped step itself: tests/scripts/test_ci_shard_matrix.py
    requires that job to hold exactly ONE pytest step so `--splits`, `--group`
    and `.test_durations` stay unambiguous. So the step lives in `contracts`,
    the non-sharded per-PR job.

    This asserts SOME per-PR job runs them, not which, so moving the step again
    does not need a test edit - only deleting it does.
    """
    jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))["jobs"]

    ignoring = [
        job_id
        for job_id, job in jobs.items()
        for step in job["steps"]
        if "--ignore=tests/analysis" in (step.get("run") or "")
    ]
    assert ignoring, (
        "fixture precondition: this test exists because a per-PR lane ignores "
        "tests/analysis. None does any more, so delete this test rather than "
        "leaving it passing for a reason that stopped being true"
    )

    hosting = [
        (job_id, step.get("run") or "")
        for job_id, job in jobs.items()
        for step in job["steps"]
        if "tests/analysis/test_jit_warmup.py" in (step.get("run") or "")
    ]
    assert hosting, (
        f"jobs {ignoring} ignore tests/analysis and no ci.yml job runs a scoped "
        "step for tests/analysis/test_jit_warmup.py, so the #1316 guards gate "
        "nothing on a PR"
    )
    for job_id, run in hosting:
        assert "not slow" in run, (
            f"ci.yml:{job_id} must deselect the cold-compile acceptance tests, "
            f"which belong in Full CI: {run!r}"
        )
        assert "passed" in run, (
            f"ci.yml:{job_id} trusts pytest's exit code. tests/analysis/"
            "conftest.py does a module-level importorskip('soundfile'), so a "
            "lane without the analysis extra skips the whole directory and "
            "still exits 0. The step must count PASSES."
        )
