"""Shared login+marker predicates for human-posted reviewer lanes.

Each lane module still owns its compiled marker regex and login set; this
module holds only the shared login+marker+SHA predicate both lanes use.

Supersedes: the duplicate is_claude_artifact / is_sol_artifact bodies in
scripts/review_claude.py and scripts/review_sol.py; those names remain as
one-statement wrappers.
"""

from __future__ import annotations

import re


def normalize_login(login: str) -> str:
    return login.removesuffix("[bot]").lower()


def is_lane_artifact(
    login: str,
    body: str,
    head_sha: str,
    logins: frozenset[str],
    marker_re: re.Pattern[str],
) -> bool:
    if normalize_login(login) not in logins:
        return False
    match = marker_re.search(body or "")
    if not match:
        return False
    return head_sha.lower().startswith(match.group(1).lower())


def is_lane_thread(
    login: str,
    body: str,
    logins: frozenset[str],
    marker_re: re.Pattern[str],
) -> bool:
    return normalize_login(login) in logins and bool(marker_re.search(body or ""))
