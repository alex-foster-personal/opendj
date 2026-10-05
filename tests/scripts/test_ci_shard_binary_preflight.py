"""Every pytest job names its host binaries before the suite runs.

Four times in three days (Fri 4 - Sun 6 Sep 2026) a trunk shard went red
minutes into the suite with a bare FileNotFoundError because the test that
shells out to a host binary happened to land on the host that lacked it:
`file`, `bwrap` (#1056), the Tauri libraries (93e11a47f), `just` (#1363).
Each PR was green because its own shard landed on the other host. The
provisioning checklist fixes the host; this makes the miss explicit, early,
and named, on every job, rather than a symptom four minutes in.

Regression lines:
  - if a pytest job drops the binary preflight then a missing host binary
    fails deep in the suite as FileNotFoundError again
  - if the preflight list drops a binary the suite shells out to then that
    binary is unguarded
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: Binaries the suite or the contracts job shells out to on a Linux runner.
#: Each entry is a red that already happened, with its provisioning fix, except
#: `ffmpeg`, which is the opposite failure and worse: `tests/conftest.py` turns
#: `requires_ffmpeg` into a SKIP, so the NATIVE-06 decoder acceptance tests did
#: not go red without it - they silently did not run, and a swapped `amerge`
#: channel or a broken filter graph could merge green (Codex review, PR #1536;
#: AGENTS.md "Never silently skip acceptance because ... a platform is
#: missing"). Naming it here means a host without it fails loudly at second
#: five instead of reporting a smaller suite as a pass.
REQUIRED = ("file", "bwrap", "just", "pkg-config", "cargo", "uv", "ffmpeg")

PYTEST_JOBS = (("ci.yml", "test"), ("ci.yml", "contracts"), ("full-ci.yml", "test"))


#: A job may also run the preflight earlier naming only its interpreter: that is
#: the stale-install guard step (tests/scripts/test_ci_workflow_stale_install_coverage.py),
#: not the host-binary preflight this module pins.
_INTERPRETER_ONLY = {"python", "python3"}


def _preflight_step(job: dict) -> dict | None:
    for step in job["steps"]:
        run = step.get("run") or ""
        if "scripts/ci_runner_preflight.sh" not in run:
            continue
        named = set(run.split("scripts/ci_runner_preflight.sh", 1)[1].split())
        if not named <= _INTERPRETER_ONLY:
            return step
    return None


def test_every_pytest_job_runs_the_binary_preflight_before_the_suite() -> None:
    """if the preflight is missing or after pytest then a missing binary reds late"""
    for workflow, job_id in PYTEST_JOBS:
        doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
        job = doc["jobs"][job_id]
        step = _preflight_step(job)
        assert step is not None, f"{workflow}:{job_id} has no ci_runner_preflight.sh step"
        run = step["run"]
        missing = [b for b in REQUIRED if f" {b}" not in run and not run.endswith(b)]
        assert not missing, f"{workflow}:{job_id} preflight does not name {missing}"
        runs = [s.get("run") or "" for s in job["steps"]]
        first_real = next(
            i for i, r in enumerate(runs)
            if ".venv/bin/pytest" in r or "cargo test" in r or "cargo nextest" in r or "make waveform" in r
        )
        assert runs.index(run) < first_real, f"{workflow}:{job_id}: preflight after the suite"
