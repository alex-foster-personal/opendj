"""setup-node's pnpm cache must never be restored onto a self-hosted runner.

On a hosted runner the store is empty and the cache is the only warm start. On
a self-hosted runner the store already persists at the runner user's
``pnpm store path``, shared by every runner service on that host (fifteen on
agentbox). ``actions/setup-node`` restores its cache tarball with absolute
paths straight into that store, ``index.db`` included, while other jobs are
reading it: trunk e14bd2f0a3 (Sat 5 Sep 2026 16:39 UTC) died in ``pnpm
install`` with ``[ERR_PNPM_ERR_SQLITE_ERROR] disk I/O error`` on a healthy
disk, and the store's ``index.db`` mtime had been pinned to the archive's
date by every restore since Thu 3 Sep.

Regression lines:
  - if any workflow that can run self-hosted passes an unconditional
    `cache: pnpm` then a cache restore can overwrite the shared store under
    a running install
  - if the guard names the wrong expression then setup-node caches on
    self-hosted anyway
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The only accepted spelling: pnpm on GitHub-hosted runners, nothing on
#: self-hosted ones, where the persistent store is the cache.
HOSTED_ONLY_CACHE = "${{ runner.environment == 'github-hosted' && 'pnpm' || '' }}"


def _setup_node_steps() -> list[tuple[str, str, dict]]:
    out = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            out.extend(
                (path.name, job_id, step)
                for step in steps
                if "actions/setup-node" in (step.get("uses") or "")
            )
    return out


def test_no_workflow_restores_a_pnpm_cache_unconditionally() -> None:
    """if a workflow passes a bare `cache: pnpm` then the shared store gets overwritten"""
    steps = _setup_node_steps()
    assert steps, "no setup-node step found; this test cannot see the workflows"
    offenders = [
        f"{wf}:{job}"
        for wf, job, step in steps
        if (step.get("with") or {}).get("cache") not in (None, HOSTED_ONLY_CACHE)
    ]
    assert not offenders, (
        "setup-node cache must be hosted-only "
        f"(`cache: {HOSTED_ONLY_CACHE}`), found: {offenders}"
    )


def test_every_pnpm_cache_is_hosted_only_not_removed() -> None:
    """CONTROL: the fix is a condition, not a deletion; hosted runners keep the cache"""
    guarded = [
        (wf, job)
        for wf, job, step in _setup_node_steps()
        if (step.get("with") or {}).get("cache") == HOSTED_ONLY_CACHE
    ]
    assert len(guarded) >= 3, (
        f"expected the frontend, e2e and packaging caches to stay, got {guarded}"
    )
