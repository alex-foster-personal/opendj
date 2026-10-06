"""Fast-forward-only preview sync helper (DEVOPS-05).

When ``previewctl sync --ff-only`` cannot fast-forward the preview worktree
from ``origin/main``, this helper refuses (leaving HEAD untouched), files one
``queue:ready`` issue naming the divergent commits, and exits non-zero.

Recovery stays the documented R2 rebuild in ``docs/ops/periodic-checks.md``;
this module never merges without ``--ff-only``, never rebases, never resets,
and never runs ``checkout -B``.

Usage::

    python -m scripts.preview_ff_sync --worktree /path/to/preview
    python -m scripts.preview_ff_sync --worktree /path/to/preview --dry-run
    python -m scripts.preview_ff_sync --worktree /path/to/preview --fetch
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts.preview_drift_git import MeasurementError

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_MAIN_REF = "origin/main"
DEFAULT_REPOSITORY = "private_owner/music-dj-tools"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNKNOWN = 2

# Recorded git argv for tests. Each entry is the full argv list passed to git.
GIT_INVOCATIONS: list[list[str]] = []


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    argv = ["git", *args]
    GIT_INVOCATIONS.append(argv)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise MeasurementError(
            f"git {' '.join(args)} failed in {cwd} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def _rev_parse(cwd: Path, ref: str) -> str:
    out = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()
    if not out:
        raise MeasurementError(f"ref does not resolve to a commit: {ref}")
    return out


def _fetch_main(cwd: Path, main_ref: str) -> None:
    """Fetch only the main branch from origin."""
    if not main_ref.startswith("origin/"):
        return
    branch = main_ref.removeprefix("origin/")
    _git(cwd, "fetch", "--quiet", "origin", branch)


def _cherry_plus(cwd: Path, main_ref: str, head_ref: str = "HEAD") -> list[str]:
    """Return SHAs from ``git cherry`` lines prefixed with ``+``."""
    proc = _git(cwd, "cherry", main_ref, head_ref, check=False)
    if proc.returncode != 0:
        raise MeasurementError(
            f"git cherry {main_ref} {head_ref} failed: {proc.stderr.strip()}"
        )
    shas: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("+ "):
            shas.append(line[2:].split()[0])
    return shas


def _cherry_subjects(cwd: Path, main_ref: str, head_ref: str = "HEAD") -> list[str]:
    proc = _git(cwd, "cherry", "-v", main_ref, head_ref, check=False)
    if proc.returncode != 0:
        raise MeasurementError(
            f"git cherry -v {main_ref} {head_ref} failed: {proc.stderr.strip()}"
        )
    subjects: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("+ "):
            # Format: + <sha> <subject>
            parts = line[2:].split(None, 1)
            if len(parts) == 2:
                subjects.append(parts[1])
    return subjects


def _log_oneline(cwd: Path, main_ref: str, head_ref: str = "HEAD") -> list[str]:
    proc = _git(cwd, "log", "--oneline", f"{main_ref}...{head_ref}", check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def fingerprint(preview_sha: str, main_sha: str) -> str:
    return f"<!-- preview-ff-refuse preview={preview_sha} main={main_sha} -->"


def build_issue_body(
    preview_sha: str,
    main_sha: str,
    cherry_subjects: list[str],
    log_lines: list[str],
) -> str:
    lines = [
        fingerprint(preview_sha, main_sha),
        "",
        "The preview worktree cannot fast-forward from main.",
        "",
        f"- preview HEAD: `{preview_sha}`",
        f"- main ref: `{main_sha}`",
        "",
        "## Preview-only commits (`git cherry`)",
    ]
    if cherry_subjects:
        lines.extend(f"- {subject}" for subject in cherry_subjects)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Divergence (`git log --oneline`)")
    if log_lines:
        lines.extend(f"- `{line}`" for line in log_lines)
    else:
        lines.append("- (none)")
    lines.extend(
        [
            "",
            "Recovery: see `docs/ops/periodic-checks.md` R2.",
            "Capture a branch first, then `checkout -B` from `origin/main`.",
            "Never `git reset --hard`.",
        ]
    )
    return "\n".join(lines)


class GitHub(Protocol):
    def find_open(self, fingerprint: str) -> int | None: ...

    def create_issue(self, title: str, body: str, labels: tuple[str, ...]) -> int: ...


class GhGitHub:
    """Production GitHub adapter using the ``gh`` CLI."""

    def __init__(self, repository: str) -> None:
        self._repository = repository

    def find_open(self, fp: str) -> int | None:
        matches: list[int] = []
        page = 1
        while True:
            output = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{self._repository}/issues",
                    "-f",
                    "state=open",
                    "-f",
                    "labels=queue:ready",
                    "-f",
                    f"per_page=100",
                    "-f",
                    f"page={page}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            issues = json.loads(output.stdout)
            if not issues:
                break
            matches.extend(
                issue["number"]
                for issue in issues
                if "pull_request" not in issue and fp in issue.get("body", "")
            )
            page += 1
        if len(matches) > 1:
            raise RuntimeError(f"multiple open issues carry fingerprint {fp!r}: {matches}")
        return matches[0] if matches else None

    def create_issue(self, title: str, body: str, labels: tuple[str, ...]) -> int:
        arguments = [
            "gh",
            "issue",
            "create",
            "--repo",
            self._repository,
            "--title",
            title,
            "--body",
            body,
        ]
        for label in labels:
            arguments.extend(("--label", label))
        completed = subprocess.run(arguments, check=True, capture_output=True, text=True)
        url = completed.stdout.strip()
        number = url.rsplit("/", maxsplit=1)[-1]
        if not number.isdecimal():
            raise RuntimeError(f"gh issue create returned an unrecognizable URL: {url!r}")
        return int(number)


@dataclass
class SyncResult:
    status: str  # "OK" | "REFUSED" | "UNKNOWN"
    preview_sha: str
    main_sha: str
    head_sha: str
    cherry: list[str] = field(default_factory=list)
    issue_number: int | None = None
    reasons: list[str] = field(default_factory=list)


def attempt_ff_sync(
    cwd: Path,
    *,
    main_ref: str = DEFAULT_MAIN_REF,
    fetch: bool = False,
    github: GitHub | None = None,
    repository: str = DEFAULT_REPOSITORY,
    dry_run: bool = False,
) -> SyncResult:
    """Attempt ``git merge --ff-only`` of ``main_ref`` into HEAD."""
    preview_sha = _rev_parse(cwd, "HEAD")

    if fetch:
        _fetch_main(cwd, main_ref)

    main_sha = _rev_parse(cwd, main_ref)
    merge_proc = _git(cwd, "merge", "--ff-only", main_ref, check=False)

    if merge_proc.returncode == 0:
        head_sha = _rev_parse(cwd, "HEAD")
        return SyncResult(
            status="OK",
            preview_sha=preview_sha,
            main_sha=main_sha,
            head_sha=head_sha,
        )

    # Refused: HEAD must remain at preview_sha
    head_sha = _rev_parse(cwd, "HEAD")
    cherry = _cherry_plus(cwd, main_ref)
    cherry_subjects = _cherry_subjects(cwd, main_ref)
    log_lines = _log_oneline(cwd, main_ref)
    body = build_issue_body(preview_sha, main_sha, cherry_subjects, log_lines)
    issue_number: int | None = None

    if not dry_run and github is not None:
        fp = fingerprint(preview_sha, main_sha)
        existing = github.find_open(fp)
        if existing is not None:
            issue_number = existing
        else:
            issue_number = github.create_issue(
                "preview cannot fast-forward from main",
                body,
                ("queue:ready",),
            )
    else:
        print(body)

    return SyncResult(
        status="REFUSED",
        preview_sha=preview_sha,
        main_sha=main_sha,
        head_sha=head_sha,
        cherry=cherry,
        issue_number=issue_number,
        reasons=[merge_proc.stderr.strip() or "merge --ff-only refused"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--main-ref", default=DEFAULT_MAIN_REF)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    args = parser.parse_args(argv)

    if not args.worktree.is_dir():
        print(f"[UNKNOWN] preview ff-sync could not measure: not a directory: {args.worktree}")
        return EXIT_UNKNOWN

    github: GitHub | None = None
    if not args.dry_run:
        github = GhGitHub(args.repository)

    try:
        result = attempt_ff_sync(
            args.worktree,
            main_ref=args.main_ref,
            fetch=args.fetch,
            github=github,
            repository=args.repository,
            dry_run=args.dry_run,
        )
    except MeasurementError as exc:
        print(f"[UNKNOWN] preview ff-sync could not measure: {exc}")
        return EXIT_UNKNOWN

    if result.status == "OK":
        print(f"[OK] preview fast-forwarded to {result.head_sha[:9]}")
        return EXIT_OK
    print(f"[REFUSED] preview cannot fast-forward from {result.main_sha[:9]}")
    if result.issue_number is not None:
        print(f"  issue: #{result.issue_number}")
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
