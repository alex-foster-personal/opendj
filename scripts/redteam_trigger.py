"""Decide whether the 07:00 watchdog tick starts a red-team attack.

Requirements:
    - [if] no build work landed since the last run [then] no pod starts
    - [if] an attack is interrupted [then] its previous SHA marker survives

The watchdog owns the once-per-day, after-07:00 schedule. This module owns the
durable SHA transaction: it writes the marker only after either a docs-only
advance or a successful attack command. It never treats a failed command as a
completed attack.

-Codex
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

try:
    from scripts import ci_health_core as health_core
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.redteam_trigger") from None
    raise
from scripts import ci_health_trunk as health_trunk
from scripts.review_docs_only import is_docs_only

DEFAULT_MARKER_PATH = Path.home() / "jobs/state/redteam-last-sha"
DEFAULT_ATTACK_COMMAND = Path.home() / "jobs/redteam/run.sh"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"


class TriggerResult(StrEnum):
    """Terminal outcomes for one watchdog tick."""

    UNCHANGED = "unchanged"
    DOCS_ONLY = "docs-only"
    ATTACKED = "attacked"
    UNVERIFIED = "unverified"


def _run_git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _remote_main_sha(repo_path: Path, remote: str, branch: str) -> str:
    output = _run_git(repo_path, "ls-remote", remote, f"refs/heads/{branch}")
    fields = output.split()
    if len(fields) != 2:
        raise RuntimeError(f"git ls-remote returned no {branch!r} SHA for {remote!r}")
    return fields[0]


def _read_marker(marker_path: Path) -> str | None:
    if not marker_path.exists():
        return None
    sha = marker_path.read_text().strip()
    if not sha:
        raise RuntimeError(f"red-team marker is empty: {marker_path}")
    return sha


def _write_marker(marker_path: Path, sha: str) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = marker_path.with_suffix(f"{marker_path.suffix}.tmp")
    temporary_path.write_text(f"{sha}\n")
    temporary_path.replace(marker_path)


def _fetch_main(repo_path: Path, remote: str, branch: str) -> None:
    _run_git(repo_path, "fetch", "--quiet", remote, branch)


def _changed_paths(repo_path: Path, previous_sha: str | None, current_sha: str) -> list[str]:
    if previous_sha is not None:
        output = _run_git(repo_path, "diff", "--name-only", f"{previous_sha}..{current_sha}")
        return [path for path in output.splitlines() if path]
    root_sha = _run_git(repo_path, "rev-list", "--max-parents=0", current_sha)
    root_paths = _run_git(
        repo_path, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", root_sha
    )
    later_paths = _run_git(repo_path, "diff", "--name-only", f"{root_sha}..{current_sha}")
    return sorted({*root_paths.splitlines(), *later_paths.splitlines()} - {""})


def _candidate_shas(repo_path: Path, previous_sha: str | None, current_sha: str) -> list[str]:
    revision = current_sha if previous_sha is None else f"{previous_sha}..{current_sha}"
    output = _run_git(repo_path, "rev-list", revision)
    candidates = [sha for sha in output.splitlines() if sha]
    if not candidates:
        raise RuntimeError(f"no commits found in red-team range {revision}")
    return candidates


def _completed_ci_runs_for_sha(sha: str) -> list[health_core.Run]:
    payload = health_core._gh_api_json(
        f"repos/{health_core.REPO}/actions/runs?head_sha={sha}&status=completed&per_page=100"
    )
    return health_core._parse_completed_runs(payload)


def newest_trunk_verified_sha(
    candidates: Sequence[str], branch: str = DEFAULT_BRANCH
) -> str | None:
    """Return the newest candidate whose completed trunk CI jobs all ran cleanly."""
    for candidate in candidates:
        runs = [
            run
            for run in _completed_ci_runs_for_sha(candidate)
            if run.name == health_trunk.GATING_WORKFLOW_NAME
            and run.event == "push"
            and run.head_branch == branch
            and run.head_sha == candidate
        ]
        if runs and health_trunk._check_trunk_verified(runs, branch).ok:
            return candidate
    return None


def _advance_marker_for_docs_only(marker_path: Path, current_sha: str) -> TriggerResult:
    _write_marker(marker_path, current_sha)
    return TriggerResult.DOCS_ONLY


def run_trigger(
    *,
    remote: str,
    marker_path: Path,
    attack: Callable[[str], None],
    verified_sha: Callable[[Sequence[str], str], str | None],
    repo_path: Path | None = None,
    branch: str = DEFAULT_BRANCH,
) -> TriggerResult:
    """Run one marker-safe trigger transaction without swallowing failed attacks."""
    repository = Path.cwd() if repo_path is None else repo_path
    current_sha = _remote_main_sha(repository, remote, branch)
    previous_sha = _read_marker(marker_path)
    if current_sha == previous_sha:
        return TriggerResult.UNCHANGED

    _fetch_main(repository, remote, branch)
    changed_paths = _changed_paths(repository, previous_sha, current_sha)
    if not changed_paths:
        return _advance_marker_for_docs_only(marker_path, current_sha)
    if is_docs_only(changed_paths):
        return _advance_marker_for_docs_only(marker_path, current_sha)

    candidates = _candidate_shas(repository, previous_sha, current_sha)
    target_sha = verified_sha(candidates, branch)
    if target_sha is None:
        return TriggerResult.UNVERIFIED
    if target_sha not in candidates:
        raise RuntimeError(f"verified SHA {target_sha} is outside the red-team range")
    attack(target_sha)
    _write_marker(marker_path, target_sha)
    return TriggerResult.ATTACKED


def _run_attack(command: str, sha: str) -> None:
    arguments = shlex.split(command)
    if not arguments:
        raise RuntimeError("red-team attack command is empty")
    subprocess.run([*arguments, sha], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER_PATH)
    parser.add_argument("--attack-command", default=str(DEFAULT_ATTACK_COMMAND))
    arguments = parser.parse_args()
    result = run_trigger(
        remote=arguments.remote,
        marker_path=arguments.marker,
        attack=lambda sha: _run_attack(arguments.attack_command, sha),
        verified_sha=newest_trunk_verified_sha,
        branch=arguments.branch,
    )
    print(f"redteam-trigger {result}")
    return 2 if result == TriggerResult.UNVERIFIED else 0


if __name__ == "__main__":
    raise SystemExit(main())
