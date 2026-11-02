"""Shared ``gh pr view`` transport for stdlib-only CI gate scripts."""

from __future__ import annotations

import json
import subprocess

DEFAULT_REPO = "private_owner/music-dj-tools"


def pr_view(
    pr: int,
    repo: str = DEFAULT_REPO,
    *,
    fields: str,
) -> dict:
    """Run ``gh pr view`` and return the requested JSON fields.

    Callers must pass ``fields`` explicitly to select the GitHub response contract.
    """
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            fields,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)
