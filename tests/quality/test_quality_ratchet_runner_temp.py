"""The quality ratchet's scratch must live under the runner's own temp dir.

scripts/quality_gate.py makes four `tempfile.mkdtemp()` dirs per run (deptry,
mypy and jscpd reports, and the merge-base worktree) and removes them in
`finally`. A cancelled or timed-out job is killed before `finally` runs, and a
persistent self-hosted runner never tidies /tmp, so each kill leaked one
~280 MB merge-base checkout. Measured Tue 29 Sep 2026: 40 leaked dirs (~13 GB)
on megamac-vm over 20 hours, which dropped it under ci_node_preflight's 10 GB
floor and failed an unrelated PR's production frontend build there.

`runner.temp` is emptied by the runner at the start of every job, so pointing
TMPDIR at it bounds the leak to one run per runner, whatever kills the job.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
RUNNER_TEMP = "${{ runner.temp }}"


def _quality_gate_steps() -> list[tuple[str, dict]]:
    return [
        (f"{path.name}:{job_id}:{step.get('name')}", step)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in ((yaml.safe_load(path.read_text()) or {}).get("jobs") or {}).items()
        for step in job.get("steps") or []
        if "scripts.quality_gate" in (step.get("run") or "")
    ]


def test_quality_ratchet_steps_exist() -> None:
    """[if] no workflow step runs scripts.quality_gate [then] broken, TMPDIR check is vacuous."""
    assert _quality_gate_steps(), "found no workflow step running scripts.quality_gate"


def test_quality_ratchet_tmpdir_is_runner_temp() -> None:
    """[if] a quality_gate step leaves TMPDIR off runner.temp [then] broken, kills leak tmp."""
    wrong = [
        f"{where}: TMPDIR={step.get('env', {}).get('TMPDIR')!r}"
        for where, step in _quality_gate_steps()
        if (step.get("env") or {}).get("TMPDIR") != RUNNER_TEMP
    ]
    assert not wrong, f"these steps must set TMPDIR: {RUNNER_TEMP}: {wrong}"
