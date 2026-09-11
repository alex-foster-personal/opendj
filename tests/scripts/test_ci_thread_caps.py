"""CI pytest jobs pin native thread pools to one thread.

Shards share a host with fifteen other runners. On trunk e9ed11ecf (Fri 4
Sep 2026) the same shard took 314 s on agentbox-4 and 636 s on nucbox-wsl-10,
and `ps` on nucbox showed why: `apps.analysis.run --backend librosa`
subprocesses spawned by the suite at 400-650 percent CPU each, OpenBLAS and
numba fanning every one out across all 32 threads while sixteen runners did
the same. One thread per process is the standard setting for a shared CI
host; the tests measure behavior, not wall time.

Regression lines:
  - if a pytest job drops a thread cap then one librosa subprocess can take
    the whole host and every other shard on it slows down
  - if the caps are set to anything but 1 then the setting is a guess about
    how many runners share the box, which nothing here can know
  - if a job that runs `apps.analysis.run` outside pytest (e2e.yml's `gate`,
    via the webui refresh route's ProcessPoolExecutor) drops a thread cap
    then a forked worker can land mid-BLAS-thread-activity under host
    contention and die outright (BrokenProcessPool), not merely run slow -
    see test_e2e_gate_pins_native_thread_pools_to_one_thread below
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

THREAD_CAPS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")

#: (workflow, job id) for every job that runs the Python suite or its tools.
PYTEST_JOBS = (
    ("ci.yml", "test"),
    ("ci.yml", "contracts"),
    ("full-ci.yml", "test"),
    # The e2e jobs boot the real engine, whose refresh job forks
    # apps.analysis.run's ProcessPoolExecutor: agentbox logged 238 python3
    # "segfault at 0 ip 0" today (Sat 5 Sep 2026), 5-20/hour all day and
    # 100-136/hour once eight e2e runners shared the host, and the Root
    # Playwright suite failed five times on BrokenProcessPool. A pool worker
    # forked while OpenBLAS/numba threads are live is the classic cause.
    ("e2e.yml", "gate"),
    ("e2e.yml", "extended"),
)

#: (workflow, job id) for jobs that run `apps.analysis.run` subprocesses
#: OUTSIDE pytest. e2e.yml's `gate` job never runs pytest, but its "Root
#: Playwright suite" step drives the webui refresh route, which shells out to
#: `apps.analysis.run --backend librosa --workers 2`
#: (apps/webui/server/routes/ingest_analysis_argv.py) on the same shared
#: self-hosted host as the pytest jobs above.
ANALYSIS_SUBPROCESS_JOBS = (("e2e.yml", "gate"),)


def _job(workflow: str, job_id: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"][job_id]


def test_every_pytest_job_pins_native_thread_pools_to_one_thread() -> None:
    """if a pytest job drops a thread cap then one subprocess can take the host"""
    for workflow, job_id in PYTEST_JOBS:
        env = _job(workflow, job_id).get("env") or {}
        for cap in THREAD_CAPS:
            assert str(env.get(cap)) == "1", (
                f"{workflow}:{job_id} must set {cap}: \"1\", got {env.get(cap)!r}"
            )


def test_e2e_gate_pins_native_thread_pools_to_one_thread() -> None:
    """if e2e.yml's gate job drops a thread cap then a forked analysis worker
    can die under host contention (BrokenProcessPool), not just run slow"""
    for workflow, job_id in ANALYSIS_SUBPROCESS_JOBS:
        env = _job(workflow, job_id).get("env") or {}
        for cap in THREAD_CAPS:
            assert str(env.get(cap)) == "1", (
                f"{workflow}:{job_id} must set {cap}: \"1\", got {env.get(cap)!r}"
            )
