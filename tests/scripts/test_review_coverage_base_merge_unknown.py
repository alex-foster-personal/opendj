"""Base-merge carry (REVIEW-16): unmeasurable reads are UNKNOWN, and triage prints the proof.

Real throwaway git repositories, see tests/scripts/base_merge_repo.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import review_coverage, review_coverage_carry
from scripts.review_claude import CLAUDE
from scripts.review_coverage import classify_reviewer
from scripts.review_coverage_carry import ReviewCarryInputs, verdicts_with_carry
from tests.scripts.base_merge_repo import (
    FILE,
    OTHER,
    PR,
    advance_main,
    attempt,
    git,
    make_repo,
    make_reviewed,
    merge_main,
    review_at,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def reviewed(repo: Path) -> str:
    return make_reviewed(repo)


@pytest.mark.requirement("REVIEW-16")
def test_missing_reviewed_object_reports_unknown_and_does_not_carry(
    repo: Path, reviewed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the reviewed head cannot be fetched [then] the MISS row says carry UNKNOWN, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)
    ghost = "0123456789abcdef0123456789abcdef01234567"
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", ghost], check=False).returncode != 0

    result = attempt(repo, ghost, head)
    assert result.verdict is None
    assert len(result.unknown) == 1 and ghost[:11] in result.unknown[0]

    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")
    inputs = ReviewCarryInputs(
        checks=[],
        evidence={},
        reviews=[review_at(ghost)],
        inline=[],
        issue_comments=[],
        repo_root=repo,
        expected_reviewers=(CLAUDE,),
        classify_reviewer=classify_reviewer,
    )
    (row,) = verdicts_with_carry(PR, head, inputs)
    assert row.reviewed is False
    assert "carry UNKNOWN" in row.reason and ghost[:11] in row.reason


@pytest.mark.requirement("REVIEW-16")
def test_shallow_clone_reports_unknown_and_does_not_carry(repo: Path, reviewed: str, tmp_path: Path) -> None:
    """[if] the checkout is a shallow clone [then] the carry reads UNKNOWN and does not carry, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)
    git(repo, "push", "-q", "origin", "pr")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "3", "--branch", "pr", f"file://{tmp_path / 'origin.git'}", str(shallow)],
        check=True,
    )
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"

    result = attempt(shallow, reviewed, head)
    assert result.verdict is None
    assert any("shallow clone" in note for note in result.unknown), result.unknown


@pytest.mark.requirement("REVIEW-16")
def test_triage_prints_base_merge_pass_line_and_proof(
    repo: Path, reviewed: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a base-merge carry passes [then] triage prints both full SHAs and the disjoint-path proof, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)
    reviews = [review_at(reviewed)]
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", repo)
    monkeypatch.setattr(review_coverage, "EXPECTED_REVIEWERS", (CLAUDE,))
    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: head)
    monkeypatch.setattr(review_coverage, "_changed_files", lambda pr: [FILE, OTHER])
    monkeypatch.setattr(review_coverage, "_checks", lambda pr: [])
    monkeypatch.setattr(
        review_coverage, "_paginated_json_list", lambda path: reviews if path.endswith("/reviews") else []
    )
    monkeypatch.setattr(review_coverage, "require_gate_current_with_main", lambda: "0" * 40)

    assert review_coverage.triage(PR) == 0

    out = capsys.readouterr().out
    reviewed_base = git(repo, "merge-base", "origin/main", reviewed)
    head_base = git(repo, "merge-base", "origin/main", head)
    assert f"ok   {CLAUDE}: carried from {reviewed[:11]} (net diff unchanged since; base merge only)" in out
    assert f"base-merge carry: reviewed {reviewed} -> head {head}" in out
    assert f"reviewed {reviewed_base}, head {head_base}" in out
    assert "disjoint paths: main changed 1, PR changed 1, shared 0" in out
    assert "net diff byte-identical at both heads, sha256 " in out
    assert "[review-coverage] PASS" in out
