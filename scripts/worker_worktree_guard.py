"""Fail-closed worker worktree creation and pull-request scope checks.

This is the repository-side contract for fleet workers. A worker must branch
from an explicit remote base, never from a mutable primary checkout.

REQUIREMENTS (OPS-41)
  R-1 Refuse an unpublished primary checkout. [done, run]
      [if] the primary is dirty or ahead of the requested base [then] preflight
      raises with the exact condition, [else stop]
  R-2 Create from a pinned base. [done, run]
      [if] a worker worktree is created [then] its branch starts at the base ref,
      [else stop]
  R-3 Surface implausible scope. [done, run]
      [if] measured commits or files exceed declared issue limits [then] the
      pre-review check fails with both measurements, [else stop]
"""

from __future__ import annotations

import argparse
import dataclasses
import subprocess
from pathlib import Path

from scripts import worktree_lifecycle


class PreflightError(RuntimeError):
    """The source checkout cannot safely create a worker branch."""


class ScopeError(RuntimeError):
    """The branch is larger than its declared issue scope."""


@dataclasses.dataclass(frozen=True)
class Scope:
    commits: int
    files: int
    additions: int
    deletions: int


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise PreflightError(f"git {' '.join(args)} failed: {process.stderr.strip()}")
    return process.stdout.strip()


def _assert_remote_tracking_base(repo: Path, base_ref: str) -> None:
    full_ref = _git(repo, "rev-parse", "--symbolic-full-name", base_ref)
    if not full_ref.startswith("refs/remotes/"):
        raise PreflightError(
            f"base ref {base_ref!r} is source-local ({full_ref}); "
            "use a fetched remote-tracking ref such as origin/main"
        )


def assert_clean_origin_base(repo: Path, base_ref: str) -> None:
    """Refuse dirty or unpublished source state before any branch is created."""
    _assert_remote_tracking_base(repo, base_ref)
    status = _git(repo, "status", "--porcelain=v1", "-uall")
    if status:
        raise PreflightError(f"primary checkout is dirty; refusing worker branch from {repo}")
    counts = _git(repo, "rev-list", "--left-right", "--count", f"{base_ref}...HEAD")
    try:
        _behind, ahead = (int(value) for value in counts.split())
    except ValueError as exc:
        raise PreflightError(f"could not measure {base_ref}...HEAD: {counts!r}") from exc
    if ahead:
        raise PreflightError(
            f"primary checkout is ahead of {base_ref} by {ahead} commit(s); "
            "push or reconcile the shared checkout before spawning a worker"
        )


def create_worker_worktree(
    repo: Path, target: Path, branch: str, base_ref: str, *, floor_gb: float | None = None
) -> None:
    """Create one worker branch, proving the source is safe first.

    `floor_gb` forwards to the lifecycle guard's own `--floor-gb`. Leave it
    None in production so the guard's real disk-floor policy applies; tests
    that are not about the disk floor pass an explicit low value so the
    guard's live `shutil.disk_usage` read of the HOST (not of `repo`'s
    content) cannot fail them on a host with little real free space.
    """
    assert_clean_origin_base(repo, base_ref)
    guard_argv = ["guard", "--repo", str(repo)]
    if floor_gb is not None:
        guard_argv += ["--floor-gb", str(floor_gb)]
    lifecycle_status = worktree_lifecycle.main(guard_argv)
    if lifecycle_status != 0:
        raise PreflightError("worktree lifecycle guard refused worker creation")
    process = subprocess.run(
        ["git", "worktree", "add", str(target), "-b", branch, base_ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise PreflightError(f"git worktree add failed: {process.stderr.strip()}")
    actual = _git(target, "rev-parse", "HEAD")
    expected = _git(repo, "rev-parse", base_ref)
    if actual != expected:
        raise PreflightError(
            f"worker worktree started at {actual[:12]}, expected {expected[:12]} from {base_ref}"
        )


def measure_scope(repo: Path, base_ref: str) -> Scope:
    """Measure the committed branch diff against the exact declared base."""
    counts = _git(repo, "rev-list", "--count", f"{base_ref}..HEAD")
    numstat = _git(repo, "diff", "--numstat", f"{base_ref}...HEAD")
    additions = deletions = 0
    files = 0
    for line in numstat.splitlines():
        columns = line.split("\t")
        if len(columns) != 3:
            raise ScopeError(f"could not parse git diff --numstat line: {line!r}")
        added, deleted = columns[:2]
        if added != "-":
            additions += int(added)
        if deleted != "-":
            deletions += int(deleted)
        files += 1
    return Scope(int(counts), files, additions, deletions)


def assert_scope(scope: Scope, *, max_commits: int, max_files: int) -> None:
    """Fail before review when branch size exceeds explicit issue limits."""
    if scope.commits > max_commits or scope.files > max_files:
        raise ScopeError(
            "branch exceeds declared issue scope: "
            f"commits={scope.commits} (max {max_commits}), "
            f"files={scope.files} (max {max_files}), "
            f"additions={scope.additions}, deletions={scope.deletions}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--repo", type=Path, required=True)
    preflight.add_argument("--base", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repo", type=Path, required=True)
    create.add_argument("--target", type=Path, required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--base", required=True)
    create.add_argument(
        "--floor-gb", type=float, default=None,
        help="override the lifecycle guard's disk floor (default: its own policy)",
    )
    scope = subparsers.add_parser("scope")
    scope.add_argument("--repo", type=Path, required=True)
    scope.add_argument("--base", required=True)
    scope.add_argument("--max-commits", type=int, required=True)
    scope.add_argument("--max-files", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        assert_clean_origin_base(args.repo, args.base)
        print(f"[worker-preflight] OK: clean source at {args.base}")
    elif args.command == "create":
        create_worker_worktree(
            args.repo, args.target, args.branch, args.base, floor_gb=args.floor_gb
        )
        print(f"[worker-worktree] OK: {args.branch} at {args.target} from {args.base}")
    elif args.command == "scope":
        measured = measure_scope(args.repo, args.base)
        assert_scope(measured, max_commits=args.max_commits, max_files=args.max_files)
        print(
            f"[worker-scope] OK: commits={measured.commits}, files={measured.files}, "
            f"additions={measured.additions}, deletions={measured.deletions}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
