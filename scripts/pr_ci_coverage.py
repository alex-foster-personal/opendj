#!/usr/bin/env python3
"""Fail when an open, non-docs pull request has no real Actions run at its head.

This runs outside ``pull_request`` because a missing pull-request delivery has
no in-workflow failure to report. It reads GitHub's live pull-request, file,
and Actions-run APIs with ``gh`` and exits nonzero for every uncovered head.

MINI-PRD
    R1 Head coverage ............................................. done + regression
       [if] an open PR changes a non-docs path and its head has no Actions run
            [then] exit 1 and name the PR and head SHA [else stop]
       [if] an open PR changes only excluded documentation paths
            [then] omit it from the required-CI denominator [else stop]
       [if] GitHub returns an incomplete or malformed API response
            [then] exit 10 with an explicit precondition error [else stop]

-Claude
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

try:
    from scripts.ci_health_core import (
        EXIT_OK,
        EXIT_PRECONDITION,
        REPO,
        PreconditionError,
        _gh_api_json,
        _run_gh,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.pr_ci_coverage") from None
    raise

# ----- configuration ---------------------------------------------------------------

PAGE_SIZE = 100
MAX_API_WORKERS = 12
EXCLUDED_DOCS_PREFIXES = ("docs/", "handoffs/", ".planning/", "specs/", "blog/")
REQUIRED_RUN_EVENTS = frozenset(
    {"pull_request", "push", "workflow_dispatch", "repository_dispatch"}
)
STATUS_CONTEXT = "PR head CI coverage"


# ----- GitHub reads ----------------------------------------------------------------


def _pages(path: str) -> Iterable[list[object]]:
    """Yield every paginated list, refusing malformed data rather than skipping it."""
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = _gh_api_json(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, list):
            raise PreconditionError(f"{path} page {page} was not a list")
        yield payload
        if len(payload) < PAGE_SIZE:
            return
        page += 1


def _open_prs() -> list[tuple[int, str]]:
    """Read every open PR with the minimum fields needed for a head check."""
    result: list[tuple[int, str]] = []
    for payload in _pages(f"repos/{REPO}/pulls?state=open"):
        for item in payload:
            if not isinstance(item, dict):
                raise PreconditionError("open pull-request listing contained a non-object")
            number = item.get("number")
            head = item.get("head")
            sha = head.get("sha") if isinstance(head, dict) else None
            if type(number) is not int or not isinstance(sha, str) or not sha:
                raise PreconditionError(
                    f"open pull-request listing has invalid number or head: {item!r}"
                )
            result.append((number, sha))
    return result


def _changed_files(pr_number: int) -> list[str]:
    """Read every changed filename for one PR, without a partial-page blind spot."""
    files: list[str] = []
    for payload in _pages(f"repos/{REPO}/pulls/{pr_number}/files"):
        for item in payload:
            filename = item.get("filename") if isinstance(item, dict) else None
            if not isinstance(filename, str) or not filename:
                raise PreconditionError(
                    f"PR #{pr_number} files response has invalid filename: {item!r}"
                )
            files.append(filename)
    if not files:
        raise PreconditionError(f"PR #{pr_number} has no changed files")
    return files


def _has_actions_run_at_head(head_sha: str) -> bool:
    """Return whether a non-create Actions workflow ran against this exact head."""
    payload = _gh_api_json(f"repos/{REPO}/actions/runs?head_sha={head_sha}&per_page={PAGE_SIZE}")
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise PreconditionError(f"Actions runs response for {head_sha} has no workflow_runs list")
    for run in payload["workflow_runs"]:
        if not isinstance(run, dict):
            raise PreconditionError(f"Actions runs response for {head_sha} contained a non-object")
        if run.get("head_sha") == head_sha and run.get("event") in REQUIRED_RUN_EVENTS:
            return True
    return False


# ----- verdict ---------------------------------------------------------------------


def _is_docs_path(path: str) -> bool:
    """Match ci.yml's docs-only exclusions, including its requirements exception."""
    if path == ".planning/REQUIREMENTS.md":
        return False
    return path.endswith(".md") or path.startswith(EXCLUDED_DOCS_PREFIXES)


def _requires_ci(files: list[str]) -> bool:
    """A PR needs CI if any changed file is outside ci.yml's docs exclusions."""
    return any(not _is_docs_path(path) for path in files)


def _inspect_pr(pr: tuple[int, str]) -> tuple[int, str, bool, bool]:
    """Return one PR's number, head, docs-only state, and head-run coverage."""
    number, head_sha = pr
    requires_ci = _requires_ci(_changed_files(number))
    return number, head_sha, requires_ci, requires_ci and _has_actions_run_at_head(head_sha)


def _publish_head_status(inspection: tuple[int, str, bool, bool]) -> None:
    """Attach an explicit success or failure status to a checked non-docs PR head."""
    number, head_sha, requires_ci, covered = inspection
    if not requires_ci:
        return
    state = "success" if covered else "failure"
    description = "Actions run found at PR head" if covered else "No non-bot Actions run at PR head"
    _run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/statuses/{head_sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={STATUS_CONTEXT}",
            "-f",
            f"description={description} for PR #{number}",
        ]
    )


def main(*, publish_status: bool) -> int:
    """Print named denominators and fail for every untested, non-docs PR head."""
    required = 0
    skipped_docs_only = 0
    uncovered: list[tuple[int, str]] = []
    prs = _open_prs()
    with ThreadPoolExecutor(max_workers=MAX_API_WORKERS) as executor:
        inspections = list(executor.map(_inspect_pr, prs))
    if publish_status:
        with ThreadPoolExecutor(max_workers=MAX_API_WORKERS) as executor:
            list(executor.map(_publish_head_status, inspections))
    for number, head_sha, requires_ci, covered in inspections:
        if not requires_ci:
            skipped_docs_only += 1
        elif covered:
            required += 1
        else:
            required += 1
            uncovered.append((number, head_sha))

    print(
        "PR_CI_COVERAGE "
        f"open_non_docs={required} docs_only_skipped={skipped_docs_only} uncovered={len(uncovered)}"
    )
    for number, head_sha in uncovered:
        print(f"[ERROR] PR #{number} head {head_sha} has no non-bot Actions run")
    return EXIT_OK if not uncovered else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish-status",
        action="store_true",
        help="publish the explicit PR-head status contexts after inspecting GitHub",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(main(publish_status=args.publish_status))
    except PreconditionError as exc:
        print(f"[ERROR] precondition: {exc}")
        raise SystemExit(EXIT_PRECONDITION) from exc
