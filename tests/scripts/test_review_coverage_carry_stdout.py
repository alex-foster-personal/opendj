"""Stdout proof when debt-only carry applies (issue #2871)."""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import review_coverage, review_coverage_carry
from scripts.review_claude import marker as claude_marker
from scripts.review_coverage_carry import debt_file_path

_PR = "2871"
_DEBT = debt_file_path(_PR)
_HEAD_LOGIN = "maintainer"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", f"touch {rel}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "rename", "origin", "upstream")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "apps/foo.py", "base\n")
    _git(work, "push", "-q", "origin", "HEAD:main")
    return work


def _claude_review_at(sha: str) -> dict:
    return {
        "user": {"login": _HEAD_LOGIN},
        "body": f"findings\n{claude_marker(sha, 'claude-test')}",
        "state": "COMMENTED",
        "commit_id": sha,
    }


# REQ: REVIEW-08
def test_triage_prints_carry_proof(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    reviews = [_claude_review_at(reviewed)]

    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", repo)
    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: head)
    monkeypatch.setattr(review_coverage, "_checks", lambda pr: [])
    monkeypatch.setattr(
        review_coverage,
        "_paginated_json_list",
        lambda path: reviews if path.endswith("/reviews") else [],
    )
    monkeypatch.setattr(
        review_coverage, "require_gate_current_with_main", lambda: "gate000000000000000000000000000000000000"
    )

    buf = StringIO()
    with patch("sys.stdout", buf):
        assert review_coverage.triage(_PR) == 0

    out = buf.getvalue()
    assert reviewed in out
    assert head in out
    assert _DEBT in out
    assert "debt-only carry:" in out
    assert f"git diff --name-only {reviewed}..{head}:" in out
    assert f"carried from {reviewed[:11]}" in out
