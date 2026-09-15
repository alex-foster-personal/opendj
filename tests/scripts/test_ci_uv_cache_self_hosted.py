"""The setup-uv action's cache must be hosted-only.

On a self-hosted runner host the persistent ``~/.cache/uv`` is shared by every
runner, and the action's post step prunes it (``uv cache prune --ci``) whenever
its cache is on. Fifteen sibling jobs hold that directory's lock, so the prune
times out and reds the job (trunk E2E on 227fb7c86, Wed 9 Sep 2026), or a
sibling's prune removes an archive entry mid ``uv sync`` (run 34275041870).

Requirements:
- [if] a workflow step uses astral-sh/setup-uv [then] its ``enable-cache`` is
  either absent (the action's ``auto`` is hosted-only) or the hosted-only
  expression, [else stop]
- [if] a step sets ``enable-cache: true`` literally [then] this test fails and
  names the workflow, job and step, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

HOSTED_ONLY = "${{ runner.environment == 'github-hosted' }}"


def _setup_uv_steps() -> list[tuple[str, str, dict]]:
    out = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            out.extend(
                (path.name, job_id, step)
                for step in job.get("steps") or []
                if "astral-sh/setup-uv" in (step.get("uses") or "")
            )
    return out


def test_setup_uv_is_used_somewhere() -> None:
    """[if] no workflow uses setup-uv [then] the guard below is vacuous, [else stop]."""
    assert _setup_uv_steps(), (
        "no astral-sh/setup-uv step found; the hosted-only guard has nothing to check"
    )


def test_no_setup_uv_cache_is_enabled_unconditionally() -> None:
    """[if] a setup-uv step caches on self-hosted runners [then] it is named, [else stop]."""
    offenders = []
    for workflow, job_id, step in _setup_uv_steps():
        value = (step.get("with") or {}).get("enable-cache")
        if value is None or str(value).strip() in {HOSTED_ONLY, "false"}:
            continue
        offenders.append(
            f"{workflow}:{job_id}:{step.get('name') or step.get('uses')} enable-cache={value!r}"
        )
    assert not offenders, (
        "setup-uv cache must be absent or hosted-only "
        f"(`enable-cache: {HOSTED_ONLY}`), found: {offenders}"
    )
