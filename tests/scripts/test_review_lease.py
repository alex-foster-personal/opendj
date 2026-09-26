"""Unit tests for reviewer lease enforcement (issue #272)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.review_gh import TriageError
from scripts.review_lease import (
    HANDOFF_LABEL,
    LeaseBlocked,
    ReviewerLease,
    active_reviewer,
    assert_update_allowed,
    emergency_notify,
    format_blocked,
    handoff,
    lease_from_pr_view,
    main,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "review_lease"
_HEAD = "abcdef0123456789abcdef0123456789abcdef01"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_active_reviewer_returns_codex_from_fixture() -> None:
    data = _load("pr_open_codex.json")
    assert active_reviewer([label["name"] for label in data["labels"]]) == "codex"


def test_active_reviewer_returns_none_after_handoff() -> None:
    data = _load("pr_handoff.json")
    assert active_reviewer([label["name"] for label in data["labels"]]) is None


def test_active_reviewer_errors_on_ambiguous_fixture() -> None:
    data = _load("pr_ambiguous.json")
    with pytest.raises(TriageError, match="ambiguous reviewer lease"):
        active_reviewer([label["name"] for label in data["labels"]])


# REQ: REVIEW-10
def test_assert_update_allowed_blocks_non_owner() -> None:
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    with pytest.raises(LeaseBlocked):
        assert_update_allowed(lease, None)


def test_assert_update_allowed_allows_owner() -> None:
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    assert_update_allowed(lease, "codex")


# REQ: REVIEW-10
def test_format_blocked_names_reviewer_and_head() -> None:
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    msg = format_blocked(lease)
    assert "reviewer:codex" in msg
    assert _HEAD in msg
    assert "MDT_REVIEWER_FLEET=codex" in msg


def test_lease_from_pr_view_builds_lease_from_fixture() -> None:
    data = _load("pr_open_codex.json")
    lease = lease_from_pr_view(str(data["number"]), data["labels"], data["headRefOid"])
    assert lease is not None
    assert lease.fleet == "codex"
    assert lease.head_sha == data["headRefOid"]


# REQ: REVIEW-10
def test_check_exits_1_naming_reviewer_and_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] PR has reviewer:codex and actor fleet is not codex [then] check exits 1."""
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=_HEAD)
    monkeypatch.setattr("scripts.review_lease.fetch_lease", lambda _pr: lease)
    monkeypatch.delenv("MDT_REVIEWER_FLEET", raising=False)
    rc = main(["check", "--pr", "1234"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "reviewer:codex" in err
    assert _HEAD in err


# REQ: REVIEW-10
def test_handoff_removes_reviewer_label_and_adds_handoff_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] handoff completes [then] no reviewer:* remains, handoff:finished-no-merge is set."""
    calls: list[list[str]] = []

    def _fake_gh(args: list[str], * _rest: object, **_kwargs: object) -> str:
        calls.append(list(args))
        if args[:2] == ["pr", "edit"]:
            return ""
        if args[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 1234,
                    "labels": [{"name": HANDOFF_LABEL}],
                }
            )
        raise AssertionError(f"unexpected gh args: {args!r}")

    monkeypatch.setattr("scripts.review_lease._gh", _fake_gh)
    monkeypatch.setattr("scripts.review_lease.pinned_head", lambda _pr: _HEAD)
    handoff("1234", "codex", None)
    edit = next(c for c in calls if c[:2] == ["pr", "edit"])
    assert "--remove-label" in edit
    assert "reviewer:codex" in edit
    assert "--add-label" in edit
    assert HANDOFF_LABEL in edit
    assert edit[edit.index("--remove-label") + 1] == "reviewer:codex"
    assert edit[edit.index("--add-label") + 1] == HANDOFF_LABEL


# REQ: REVIEW-10
def test_emergency_notify_lists_each_new_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] emergency bypass runs [then] the PR comment lists each new commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "lease-head commit"], check=True)
    lease_head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    (repo / "file.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first new commit"], check=True)
    (repo / "file.txt").write_text("three\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second new commit"], check=True)
    intended = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    captured: dict[str, str] = {}

    def _fake_gh(args: list[str], * _rest: object, **_kwargs: object) -> str:
        captured["args"] = " ".join(args)
        if "--body" in args:
            captured["body"] = args[args.index("--body") + 1]
        return ""

    monkeypatch.chdir(repo)
    monkeypatch.setattr("scripts.review_lease._gh", _fake_gh)
    lease = ReviewerLease(pr="1234", fleet="codex", label="reviewer:codex", head_sha=lease_head)
    emergency_notify("1234", lease, intended)
    body = captured["body"]
    assert "EMERGENCY reviewer-lease bypass" in body
    assert "first new commit" in body
    assert "second new commit" in body
    assert "pr comment 1234" in captured["args"]
