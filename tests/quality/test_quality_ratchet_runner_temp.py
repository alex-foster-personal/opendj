"""The quality ratchet's scratch must live in a per-runner dir it wipes itself.

scripts/quality_gate.py makes four `tempfile.mkdtemp()` dirs per run (deptry,
mypy and jscpd reports, and the merge-base worktree) and removes them in
`finally`. A cancelled or timed-out job is killed before `finally` runs, and a
persistent self-hosted runner never tidies /tmp, so each kill leaked one
~280 MB merge-base checkout. Measured Tue 29 Sep 2026: 40 leaked dirs (~13 GB)
on megamac-vm over 20 hours, which dropped it under ci_node_preflight's 10 GB
floor and failed an unrelated PR's production frontend build there.

A per-runner dir under `runner.workspace`, wiped at the start of the step,
bounds the leak to one run per runner, whatever kills the job, and stays on
the checkout's disk. `runner.temp` would also be emptied per job, but
nucbox-wsl-10..14 mount it as a 2 GB tmpfs (codex review on #4442).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
SCRATCH = "${{ runner.workspace }}/quality-gate-tmp"
WIPE = 'rm -rf "$TMPDIR" && mkdir -p "$TMPDIR"'


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


def test_quality_ratchet_tmpdir_is_wiped_workspace_dir() -> None:
    """[if] a quality_gate step keeps TMPDIR unwiped or off-disk [then] broken, kills leak."""
    wrong = [
        f"{where}: TMPDIR={(step.get('env') or {}).get('TMPDIR')!r}"
        for where, step in _quality_gate_steps()
        if (step.get("env") or {}).get("TMPDIR") != SCRATCH
        or step["run"].lstrip().splitlines()[0].strip() != WIPE
    ]
    assert not wrong, f"these steps must set TMPDIR: {SCRATCH} and run {WIPE!r} first: {wrong}"
