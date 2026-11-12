"""Hermetic REST fallback for review tooling `gh pr` reads (GB-22 / fleet-af#758)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.review_gh import TriageError, _gh_repo_env, _pull_to_gh_view, _resolve_repo, pulls_with_head_branch

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "review_gh" / "pr_view_rest.json"
_LANE_TOOL_PATHS = (
    Path("scripts/sol_review.py"),
    Path("scripts/review_lease.py"),
    Path("scripts/review_lane.py"),
    Path("scripts/review_pr_diff.py"),
)
_FORBIDDEN_SNIPPETS = (
    '["pr", "view"',
    "['pr', 'view'",
    '["pr", "list"',
    "['pr', 'list'",
    '["pr", "comment"',
    "['pr', 'comment'",
    '["pr", "edit"',
    "['pr', 'edit'",
)


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_review_lane_tools_do_not_call_gh_pr_directly() -> None:
    """Re-introducing `gh pr view` (or list/comment/edit) in lane tools must fail."""
    root = Path(__file__).resolve().parents[2]
    for rel in _LANE_TOOL_PATHS:
        text = (root / rel).read_text(encoding="utf-8")
        for snippet in _FORBIDDEN_SNIPPETS:
            assert snippet not in text, f"{rel} must route through review_gh helpers, found {snippet!r}"


@pytest.mark.requirement("REVIEW-10")
def test_pull_to_gh_view_reads_the_recorded_rest_pull() -> None:
    """[if] a REST pulls/{n} payload is mapped [then] it has the gh pr view shape, [else stop]."""
    pull = _load_fixture()["pull"]
    view = _pull_to_gh_view(
        pull,
        ("number", "state", "headRefOid", "headRefName", "baseRefOid", "baseRefName", "labels"),
    )
    assert view == {
        "number": 1234,
        "state": "OPEN",
        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
        "headRefName": "af--fixture-branch",
        "baseRefOid": "1111111111111111111111111111111111111111",
        "baseRefName": "main",
        "labels": [{"name": "reviewer:codex"}],
    }


def test_pull_to_gh_view_returns_only_the_requested_base_name() -> None:
    """[if] one field is asked for [then] only that field comes back, [else stop]."""
    assert _pull_to_gh_view(_load_fixture()["pull"], ("baseRefName",)) == {"baseRefName": "main"}


def test_pull_to_gh_view_refuses_a_field_it_cannot_map() -> None:
    """[if] a caller asks for a field REST has no mapping for [then] it fails loud, [else stop]."""
    with pytest.raises(TriageError, match="mergeStateStatus"):
        _pull_to_gh_view(_load_fixture()["pull"], ("number", "mergeStateStatus"))


@pytest.mark.requirement("REVIEW-8386")
def test_gh_repo_env_wins_over_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] GH_REPO names the upstream but helpers target origin [then] broken, [else stop]."""
    monkeypatch.setenv("GH_REPO", "upstream-org/music-dj-tools")
    assert _resolve_repo(None) == "upstream-org/music-dj-tools"
    assert _resolve_repo("explicit/repo") == "explicit/repo"


@pytest.mark.requirement("REVIEW-8386")
def test_gh_repo_env_accepts_a_host_prefix_and_rejects_junk() -> None:
    """[if] GH_REPO=HOST/OWNER/REPO is misread, or junk passes [then] broken, [else stop]."""
    assert _gh_repo_env("github.com/a/b") == "a/b"
    assert _gh_repo_env("  ") is None
    with pytest.raises(TriageError):
        _gh_repo_env("only-one-part")


@pytest.mark.requirement("REVIEW-8386")
def test_a_fork_head_branch_is_still_found() -> None:
    """[if] a fork's open PR on the leased branch is dropped [then] broken (the lease goes unenforced), [else stop]."""
    pulls = [
        {"number": 1, "head": {"ref": "feature", "repo": {"owner": {"login": "fork-user"}}}},
        {"number": 2, "head": {"ref": "other", "repo": {"owner": {"login": "upstream-org"}}}},
        {"number": 3, "head": {"ref": "feature", "repo": {"owner": {"login": "upstream-org"}}}},
    ]
    assert [pull["number"] for pull in pulls_with_head_branch(pulls, "feature")] == [1, 3]
