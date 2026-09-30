"""CI pytest scratch must live in the per-job RUNNER_TEMP, not /tmp.

The Wed 30 Sep 2026 SSD wear audit measured the self-hosted CI hosts writing
3.3-3.9 TB/day each (agentbox, agbox2, agbox3) and 2.5 TB/day at nucbox's
NVMe. Per-runner cgroup io.stat on agentbox attributed ~58% of it to the
`pytest fast lane` and `pytest fast tier` jobs, and the files those jobs held
open for writing were their `--basetemp` and `tempfile` dirs under /tmp
(cloudsync-sim-*, Chromium profiles). /tmp also kept whatever a killed job
left: 15 GB and 24k entries on agentbox, 25 GB on agbox2 after 2.5 days.

Both pytest steps therefore pass `--basetemp` under RUNNER_TEMP and set
TMPDIR to `runner.temp`. The runner empties that dir at every job start, and
the hosts mount it as tmpfs, so the churn never reaches flash.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github/workflows/ci.yml"
TMPDIR = "${{ runner.temp }}"
STEP_IDS = {"test": "pytest-shard", "fast": "fast"}


def _step(job_id: str, step_id: str) -> dict:
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    return next(s for s in jobs[job_id]["steps"] if s.get("id") == step_id)


def test_pytest_steps_exist_and_use_runner_temp_basetemp() -> None:
    """[if] a pytest step is gone or its --basetemp leaves RUNNER_TEMP [then] broken, check is vacuous."""
    for job_id, step_id in STEP_IDS.items():
        run = _step(job_id, step_id)["run"]
        assert "--basetemp=\"${RUNNER_TEMP:?RUNNER_TEMP must be set}/" in run, (job_id, step_id)


def test_pytest_steps_point_tmpdir_at_runner_temp() -> None:
    """[if] a pytest step's TMPDIR is not runner.temp [then] broken, scratch leaks to /tmp on the SSD."""
    wrong = {
        f"{job_id}/{step_id}": (_step(job_id, step_id).get("env") or {}).get("TMPDIR")
        for job_id, step_id in STEP_IDS.items()
        if (_step(job_id, step_id).get("env") or {}).get("TMPDIR") != TMPDIR
    }
    assert not wrong, f"TMPDIR must be {TMPDIR}: {wrong}"
