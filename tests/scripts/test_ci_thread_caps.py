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
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

THREAD_CAPS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")

#: (workflow, job id) for every job that runs the Python suite or its tools.
PYTEST_JOBS = (("ci.yml", "test"), ("ci.yml", "contracts"), ("full-ci.yml", "test"))


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
