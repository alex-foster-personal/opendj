"""Resolve the bundle-budget charge ids a lander bundle train owns: its members'.

A lander bundle train (head ref ``lander-train/<id>``, author
``opendj-devops-trunk-merge[bot]``) is one draft PR that merges several member PRs onto
main for a single CI run. The bundle gate charges a PR against the charge files whose ids
are that PR's number and head branch, so the train's own number and ``lander-train/``
ref own nothing and every member's growth read as over an allowance of 0 (9 of 9 trains
red from 08:00Z Thu 12 Nov 2026, e.g. #13478). This helper reads the member lines
``- #N at <sha>`` from the train body, resolves each member's head branch through the
GitHub REST API, and writes ``ids=<N>,<branch>,...`` to ``$GITHUB_OUTPUT``. A body with
no member lines, or a member whose branch cannot be read, fails the step: the allowance is
never guessed as 0 or as unlimited. The workflow runs it only for a trusted train (same
check as #13443's requirement-id skip); every other PR keeps ``<number>,<head_ref>``.

Usage: ``python3 -m scripts.ci_bundle_train_own_ids``. Required env: ``PR_BODY``,
``GH_TOKEN``, ``GITHUB_REPOSITORY``, ``GITHUB_OUTPUT``; optional ``GITHUB_API_URL``.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

#: A member line in the lander's train body: ``- #13453 at 620e202f16bf``.
MEMBER_LINE = re.compile(r"^\s*-\s+#(\d+)\s+at\s+[0-9a-f]{7,40}\s*$", re.MULTILINE)

HeadRefLookup = Callable[[int], str]


class TrainOwnIdsError(Exception):
    """The train body names no members, a member branch is unreadable, or env is missing."""


def parse_member_numbers(body: str) -> list[int]:
    """Member PR numbers from the train body, in order, without duplicates."""
    seen: list[int] = []
    for match in MEMBER_LINE.finditer(body):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    if not seen:
        raise TrainOwnIdsError(
            "the train PR body lists no members (no '- #N at <sha>' lines); refusing to judge "
            "its bundle growth against an allowance of 0 or unlimited"
        )
    return seen


def own_ids_for_members(numbers: list[int], head_ref_of: HeadRefLookup) -> str:
    """Comma-joined ``<number>,<head branch>`` for every member, the form ``--own`` reads."""
    ids: list[str] = []
    for number in numbers:
        ref = head_ref_of(number)
        if not ref:
            raise TrainOwnIdsError(f"member #{number} has no head branch")
        ids += [str(number), ref]
    return ",".join(ids)


def github_head_ref_lookup(api_url: str, repository: str, token: str) -> HeadRefLookup:
    """A lookup that reads ``pulls/<n>.head.ref`` from the GitHub REST API."""

    def head_ref_of(number: int) -> str:
        request = urllib.request.Request(
            f"{api_url}/repos/{repository}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TrainOwnIdsError(f"could not read member #{number} from the GitHub API: {exc}") from exc
        return str(payload.get("head", {}).get("ref", ""))

    return head_ref_of


def _annotation(kind: str, body: str) -> str:
    return f"::{kind} title=Bundle budgets::{body}"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise TrainOwnIdsError(f"{name} must be set")
    return value


def main() -> int:
    """Read the train body + token from env, write ids= to GITHUB_OUTPUT."""
    try:
        output = _require_env("GITHUB_OUTPUT")
        body = os.environ.get("PR_BODY", "")
        numbers = parse_member_numbers(body)
        lookup = github_head_ref_lookup(
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            _require_env("GITHUB_REPOSITORY"),
            _require_env("GH_TOKEN"),
        )
        ids = own_ids_for_members(numbers, lookup)
    except TrainOwnIdsError as exc:
        print(_annotation("error", str(exc)))
        return 1
    print(_annotation("notice", f"lander bundle train: charged against its members' charge files ({ids})"))
    with Path(output).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"ids={ids}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
