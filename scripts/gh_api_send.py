"""Shared `gh api -X` JSON-body sender for CI data publishers.

Supersedes: the duplicate `_gh_send` bodies in
`scripts/ci_durations_ledger.py` and `scripts/e2e_footprints_refresh.py`;
those names remain as one-statement wrappers that bind LedgerError / MapError.
"""

from __future__ import annotations

import json
import shutil
import subprocess


def gh_send(
    method: str,
    path: str,
    body: dict,
    timeout: float = 120,
    *,
    error: type[Exception],
) -> bytes:
    gh = shutil.which("gh")
    if gh is None:
        raise error("gh is not on PATH")
    done = subprocess.run(
        [gh, "api", "-X", method, path, "--input", "-"],
        input=json.dumps(body).encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode != 0:
        raise error(
            f"gh api -X {method} {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout
