"""Paginated fallback when `gh pr diff` hits GitHub's 300-file cap (issue #2546).

The ci-fixer worker needs diff-like prompt context for oversized PRs. This module
keeps the normal `gh pr diff` path byte-for-byte and only reconstructs context from
the pulls/files API when that call fails with the known HTTP 406 cap signature.

-Cursor
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from scripts.ci_health_core import PreconditionError

_DIFF_CAP_MARKERS = (
    "http 406",
    "the diff exceeded the maximum number of files",
)


def _is_diff_cap_error(exc: PreconditionError) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _DIFF_CAP_MARKERS)


def _flatten_pages(pages: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [item for page in pages for item in page]


def _validate_file_records(
    payload: object, *, pr_number: int
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise PreconditionError(
            f"PR #{pr_number} files response was not a list of pages: {payload!r}"
        )
    records: list[dict[str, Any]] = []
    for page_index, page in enumerate(payload, start=1):
        if not isinstance(page, list):
            raise PreconditionError(
                f"PR #{pr_number} files page {page_index} was not a list: {page!r}"
            )
        for item_index, item in enumerate(page, start=1):
            if not isinstance(item, dict):
                raise PreconditionError(
                    "PR #"
                    f"{pr_number} files page {page_index} item {item_index} "
                    f"was not an object: {item!r}"
                )
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename:
                raise PreconditionError(
                    "PR #"
                    f"{pr_number} files page {page_index} item {item_index} "
                    f"has invalid filename: {item!r}"
                )
            records.append(item)
    if not records:
        raise PreconditionError(f"PR #{pr_number} files response contained no records")
    return records


def _patch_unavailable_marker(record: dict[str, Any]) -> str:
    counts: list[str] = []
    for key in ("additions", "deletions", "changes"):
        value = record.get(key)
        if isinstance(value, int):
            counts.append(f"{key}={value}")
    suffix = f" ({', '.join(counts)})" if counts else ""
    return f"[patch unavailable from GitHub files API{suffix}]"


def _git_diff_paths(left: str, right: str) -> tuple[str, str, str, str]:
    """Return diff --git and ---/+++ path tokens in git's usual shape."""
    left_git = "dev/null" if left == "/dev/null" else left
    right_git = "dev/null" if right == "/dev/null" else right
    left_marker = "/dev/null" if left == "/dev/null" else f"a/{left}"
    right_marker = "/dev/null" if right == "/dev/null" else f"b/{right}"
    return (
        f"a/{left_git}",
        f"b/{right_git}",
        left_marker,
        right_marker,
    )


def _render_file_diff(record: dict[str, Any]) -> str:
    filename = record["filename"]
    status = record.get("status")
    status_text = status if isinstance(status, str) and status else "changed"
    previous = record.get("previous_filename")
    previous_name = previous if isinstance(previous, str) and previous else filename

    if status_text == "added":
        left_path, right_path = "/dev/null", filename
    elif status_text == "removed":
        left_path, right_path = filename, "/dev/null"
    elif status_text == "renamed":
        left_path, right_path = previous_name, filename
    else:
        left_path, right_path = filename, filename

    left_git, right_git, left_marker, right_marker = _git_diff_paths(left_path, right_path)
    lines = [
        f"diff --git {left_git} {right_git}",
        f"status: {status_text}",
        f"--- {left_marker}",
        f"+++ {right_marker}",
    ]

    patch = record.get("patch")
    if isinstance(patch, str) and patch:
        lines.append(patch.rstrip("\n"))
    else:
        lines.append(_patch_unavailable_marker(record))
    return "\n".join(lines)


def _render_reconstructed_diff(records: list[dict[str, Any]]) -> str:
    return "\n".join(_render_file_diff(record) for record in records)


def _fetch_files_api_diff(
    pr_number: int,
    repo: str,
    run_gh: Callable[[list[str]], str],
) -> str:
    endpoint = f"repos/{repo}/pulls/{pr_number}/files?per_page=100"
    raw = run_gh(["api", endpoint, "--paginate", "--slurp"])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreconditionError(
            f"gh api {endpoint} returned unparseable JSON: {exc}"
        ) from exc
    records = _validate_file_records(payload, pr_number=pr_number)
    return _render_reconstructed_diff(records)


def fetch_pr_diff_with_fallback(
    pr_number: int,
    repo: str,
    run_gh: Callable[[list[str]], str],
) -> str:
    """Return unified diff text, falling back to reconstructed files API context."""
    try:
        return run_gh(["pr", "diff", str(pr_number)])
    except PreconditionError as exc:
        if not _is_diff_cap_error(exc):
            raise
    return _fetch_files_api_diff(pr_number, repo, run_gh)
