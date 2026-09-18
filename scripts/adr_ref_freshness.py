"""Repository-ref freshness probes for :mod:`scripts.adr_check` (issue #3513)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STALE_MERGE_REF_MSG = "merge ref is stale, update your branch"


@dataclass(frozen=True)
class RefVerdict:
    code: int
    message: str


def _git_rev_parse(ref: str, repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse {ref} failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def emit_gate_result(code: int, message: str) -> int:
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


def check_ref_freshness(base_ref: str, head_ref: str, repo_root: Path) -> RefVerdict | None:
    """Return a failing verdict when ``base_ref`` is not an ancestor of ``head_ref``."""
    try:
        base_sha = _git_rev_parse(base_ref, repo_root)
        head_sha = _git_rev_parse(head_ref, repo_root)
    except Exception as exc:
        return RefVerdict(2, f"[adr-check] UNKNOWN: could not resolve refs ({exc})")

    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RefVerdict(
            2,
            "[adr-check] UNKNOWN: ancestry probe failed "
            f"(git merge-base --is-ancestor {base_ref} {head_ref}: {exc})",
        )
    if proc.returncode == 0:
        return None
    if proc.returncode == 1:
        return RefVerdict(
            1,
            "[adr-check] "
            f"{STALE_MERGE_REF_MSG} "
            f"(base {base_ref}={base_sha[:12]} is not an ancestor of "
            f"{head_ref}={head_sha[:12]})",
        )
    return RefVerdict(
        2,
        "[adr-check] UNKNOWN: ancestry probe failed "
        f"(git merge-base --is-ancestor {base_ref} {head_ref} "
        f"rc={proc.returncode}: {proc.stderr.strip()})",
    )
