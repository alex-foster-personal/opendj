"""The lockfile check is hosted-only and gated on the account's hosted billing.

- [if] GitHub-hosted Actions are blocked on billing [then] the job is skipped, never
  a failure that reads as a lockfile verdict, [else stop]
- [if] the gate variable is set [then] the job runs on ubuntu-latest exactly as before,
  [else stop]
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "lockfile-check.yml"
GATE = "vars.CI_HOSTED_LOCKFILE_CHECK == 'true'"


def test_lockfile_check_is_gated_on_hosted_billing() -> None:
    """if the gate is dropped then a billing block reads as a red lockfile verdict"""
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["uv-lock"]
    condition = " ".join(str(job.get("if", "")).split())
    assert GATE in condition, (
        "the hosted lockfile job must skip while hosted billing is blocked, "
        f"not fail with no runner:\n{condition!r}"
    )
    assert job["runs-on"] == "ubuntu-latest", "the check stays hosted (sdist exposure)"
    assert "uv lock --check" in job["steps"][-1]["run"]
