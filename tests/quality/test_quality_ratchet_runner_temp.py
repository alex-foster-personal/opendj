"""The quality ratchet's scratch must live in a per-runner dir its job wipes first.

scripts/quality_gate.py makes four `tempfile.mkdtemp()` dirs per run (deptry,
mypy and jscpd reports, and the merge-base worktree) and removes them in
`finally`. A cancelled or timed-out job is killed before `finally` runs, and a
persistent self-hosted runner never tidies /tmp, so each kill leaked one
~280 MB merge-base checkout. Measured Tue 29 Sep 2026: 40 leaked dirs (~13 GB)
on megamac-vm over 20 hours, which dropped it under ci_node_preflight's 10 GB
floor and failed an unrelated PR's production frontend build there.

The job names one dir beside the checkout (`github.workspace/..`), on the
checkout's disk, and wipes it as its first step, which bounds the leak to one
run per runner whatever kills the job. The wipe runs before the disk preflight,
so a leftover cannot trip the floor that would block its own cleanup.
`runner.temp` would also be emptied per job, but nucbox-wsl-10..14 mount it as
a 2 GB tmpfs (codex review on #4442).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
SCRATCH = "${{ github.workspace }}/../quality-gate-tmp"
TMPDIR = "${{ env.QUALITY_GATE_TMP }}"
WIPE = 'rm -rf "$QUALITY_GATE_TMP" && mkdir -p "$QUALITY_GATE_TMP"'


def _quality_gate_jobs() -> list[tuple[str, dict, int]]:
    """Every (where, job, step index) whose step runs scripts.quality_gate."""
    return [
        (f"{path.name}:{job_id}", job, i)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in ((yaml.safe_load(path.read_text()) or {}).get("jobs") or {}).items()
        for i, step in enumerate(job.get("steps") or [])
        if "scripts.quality_gate" in (step.get("run") or "")
    ]


def _problems(where: str, job: dict, gate_index: int) -> list[str]:
    steps = job["steps"]
    wipes = [i for i, s in enumerate(steps) if (s.get("run") or "").strip() == WIPE]
    preflights = [i for i, s in enumerate(steps) if "ci_node_preflight" in (s.get("run") or "")]
    problems = []
    if (job.get("env") or {}).get("QUALITY_GATE_TMP") != SCRATCH:
        problems.append(f"{where}: job env QUALITY_GATE_TMP must be {SCRATCH}")
    if (steps[gate_index].get("env") or {}).get("TMPDIR") != TMPDIR:
        problems.append(f"{where}: the gate step's TMPDIR must be {TMPDIR}")
    if not wipes or wipes[0] > min([gate_index, *preflights]):
        problems.append(f"{where}: {WIPE!r} must run before the disk preflight and the gate")
    return problems


def test_quality_ratchet_steps_exist() -> None:
    """[if] no workflow step runs scripts.quality_gate [then] broken, TMPDIR check is vacuous."""
    assert _quality_gate_jobs(), "found no workflow step running scripts.quality_gate"


def test_quality_ratchet_scratch_is_wiped_before_preflight() -> None:
    """[if] ratchet scratch is off-disk or wiped after preflight [then] broken, kills leak."""
    problems = [p for where, job, i in _quality_gate_jobs() for p in _problems(where, job, i)]
    assert not problems, problems
