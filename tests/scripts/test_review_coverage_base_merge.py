"""Base-merge carry for reviewer coverage (REVIEW-12).

Real throwaway git repositories in tmp_path: real commits, real merges, a real
bare `origin` whose main is pinned by `ls-remote`, real merge-base and
patch-id. No mocks of git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import review_coverage_carry
from scripts.review_claude import CLAUDE
from scripts.review_claude import marker as claude_marker
from scripts.review_coverage import classify_reviewer
from scripts.review_coverage_base_merge import net_diff, patch_id_of
from scripts.review_coverage_carry import ReviewCarryInputs, carry_attempt, verdicts_with_carry

_PR = "4426"
_FILE = "apps/foo.py"
_OTHER = "apps/other.py"
_LOGIN = "maintainer"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def _lines(edits: dict[int, str] | None = None) -> str:
    rows = [f"line{i}" for i in range(40)]
    for index, text in (edits or {}).items():
        rows[index] = text
    return "\n".join(rows) + "\n"


def _commit(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", f"touch {rel}")
    return _git(root, "rev-parse", "HEAD")


def _advance_main(root: Path, rel: str, text: str) -> None:
    """Land a commit on origin's main, then return to the PR branch."""
    _git(root, "checkout", "-q", "main")
    _commit(root, rel, text)
    _git(root, "push", "-q", "origin", "main")
    _git(root, "checkout", "-q", "pr")


def _merge_main(root: Path) -> str:
    _git(root, "merge", "-q", "--no-edit", "main")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    _commit(work, _FILE, _lines())
    _commit(work, _OTHER, "other v1\n")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "pr")
    return work


@pytest.fixture
def reviewed(repo: Path) -> str:
    """The PR's own change, reviewed by Claude at this head."""
    return _commit(repo, _FILE, _lines({30: "line30 pr"}))


def _review_at(sha: str) -> dict:
    return {
        "user": {"login": _LOGIN},
        "body": f"findings\n{claude_marker(sha, 'claude-test')}",
        "state": "COMMENTED",
        "commit_id": sha,
    }


def _attempt(repo: Path, reviewed_sha: str, head: str):
    return carry_attempt(CLAUDE, _PR, head, [_review_at(reviewed_sha)], [], [], repo)


# ----------------------------------------------------------------------------
# (a) carries


@pytest.mark.requirement("REVIEW-12")
def test_clean_base_merge_carries_naming_sha_and_patch_id(repo: Path, reviewed: str) -> None:
    """[if] a clean base merge leaves the net diff unchanged [then] coverage carries, [else stop]."""
    _advance_main(repo, _OTHER, "other v2\n")
    head = _merge_main(repo)

    attempt = _attempt(repo, reviewed, head)
    carried = attempt.verdict
    assert carried is not None and carried.reviewed is True
    assert carried.reason == f"carried from {reviewed[:11]} (net diff unchanged since; base merge only)"
    assert carried.carried_from == reviewed
    proof = "\n".join(carried.carry_proof)
    assert reviewed in proof and head in proof
    tip = _git(repo, "rev-parse", "origin/main")
    assert net_diff(repo, tip, head).patch_id in proof


@pytest.mark.requirement("REVIEW-12")
def test_main_change_far_from_pr_hunk_in_same_file_carries(repo: Path, reviewed: str) -> None:
    """[if] main edits the PR's file outside the reviewed hunk's context [then] the
    patch-id ignores the line shift and coverage carries, [else stop]."""
    _advance_main(repo, _FILE, _lines({2: "line2 main"}))
    head = _merge_main(repo)
    assert _git(repo, "show", f"{head}:{_FILE}").splitlines()[2] == "line2 main"

    assert _attempt(repo, reviewed, head).verdict is not None


@pytest.mark.requirement("REVIEW-12")
def test_main_change_inside_pr_hunk_context_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] main edits a context line of the reviewed hunk [then] the net diff changes
    and coverage does not carry, [else stop]. Decided, not incidental: the reviewer
    read that context line as it was."""
    _advance_main(repo, _FILE, _lines({28: "line28 main"}))
    head = _merge_main(repo)

    attempt = _attempt(repo, reviewed, head)
    assert attempt.verdict is None and attempt.unknown == ()


# ----------------------------------------------------------------------------
# (b), (c) and the revert pair: the PR's own change moved, so no carry


@pytest.mark.requirement("REVIEW-12")
def test_own_commit_on_pr_file_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] a commit after review changes the PR's own file [then] no carry, [else stop]."""
    _commit(repo, _FILE, _lines({30: "line30 pr", 31: "line31 pr"}))
    _advance_main(repo, _OTHER, "other v2\n")
    head = _merge_main(repo)

    attempt = _attempt(repo, reviewed, head)
    assert attempt.verdict is None and attempt.unknown == ()


@pytest.mark.requirement("REVIEW-12")
def test_conflict_resolution_that_alters_net_diff_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] a conflicted base merge is resolved into a different net diff [then] no carry, [else stop]."""
    _advance_main(repo, _FILE, _lines({30: "line30 main"}))
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "-q", "--no-edit", "main"], capture_output=True, text=True, check=False
    )
    assert merge.returncode != 0, "the fixture must produce a real conflict"
    (repo / _FILE).write_text(_lines({30: "line30 main and pr"}), encoding="utf-8")
    _git(repo, "add", _FILE)
    _git(repo, "commit", "-q", "--no-edit")
    head = _git(repo, "rev-parse", "HEAD")
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", head).split()) == 3

    attempt = _attempt(repo, reviewed, head)
    assert attempt.verdict is None and attempt.unknown == ()


@pytest.mark.requirement("REVIEW-12")
def test_revert_pair_then_base_merge_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] own commits that net to nothing ride along with a base merge [then] no carry,
    because the PASS line claims base merge only, [else stop]."""
    _commit(repo, _FILE, _lines({30: "line30 pr", 5: "line5 sneaky"}))
    _commit(repo, _FILE, _lines({30: "line30 pr"}))
    _advance_main(repo, _OTHER, "other v2\n")
    head = _merge_main(repo)

    attempt = _attempt(repo, reviewed, head)
    assert attempt.verdict is None and attempt.unknown == ()


@pytest.mark.requirement("REVIEW-12")
def test_whitespace_only_change_moves_the_patch_id(repo: Path) -> None:
    """[if] re-indenting a reviewed line keeps the patch-id [then broken]: `--stable`
    alone strips whitespace before hashing, `--verbatim` does not."""
    base = _git(repo, "rev-parse", "HEAD")
    plain = _commit(repo, _FILE, _lines({30: "line30 pr"}))
    indented = _commit(repo, _FILE, _lines({30: "    line30 pr"}))

    def pid(sha: str) -> str:
        return patch_id_of(
            repo,
            subprocess.run(
                ["git", "-C", str(repo), "diff", "--binary", base, sha], capture_output=True, check=True
            ).stdout,
        )

    assert pid(plain) != pid(indented)


# ----------------------------------------------------------------------------
# (d) unmeasurable is UNKNOWN, never a carry


@pytest.mark.requirement("REVIEW-12")
def test_missing_reviewed_object_reports_unknown_and_does_not_carry(
    repo: Path, reviewed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the reviewed head cannot be fetched [then] the MISS row says carry UNKNOWN, [else stop]."""
    _advance_main(repo, _OTHER, "other v2\n")
    head = _merge_main(repo)
    ghost = "0123456789abcdef0123456789abcdef01234567"
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", ghost], check=False).returncode != 0

    attempt = _attempt(repo, ghost, head)
    assert attempt.verdict is None
    assert len(attempt.unknown) == 1 and ghost[:11] in attempt.unknown[0]

    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")
    inputs = ReviewCarryInputs(
        checks=[],
        evidence={},
        reviews=[_review_at(ghost)],
        inline=[],
        issue_comments=[],
        repo_root=repo,
        expected_reviewers=(CLAUDE,),
        classify_reviewer=classify_reviewer,
    )
    (row,) = verdicts_with_carry(_PR, head, inputs)
    assert row.reviewed is False
    assert "carry UNKNOWN" in row.reason and ghost[:11] in row.reason


@pytest.mark.requirement("REVIEW-12")
def test_shallow_clone_reports_unknown_and_does_not_carry(repo: Path, reviewed: str, tmp_path: Path) -> None:
    """[if] a shallow clone renders a base-merge verdict [then broken]: merge-base needs history."""
    _advance_main(repo, _OTHER, "other v2\n")
    head = _merge_main(repo)
    _git(repo, "push", "-q", "origin", "pr")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "3", "--branch", "pr", f"file://{tmp_path / 'origin.git'}", str(shallow)],
        check=True,
    )
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(repo, "rev-parse", "HEAD") == head

    attempt = _attempt(shallow, reviewed, head)
    assert attempt.verdict is None
    assert any("shallow clone" in note for note in attempt.unknown), attempt.unknown


# ----------------------------------------------------------------------------
# triage stdout: the PASS row and the proof block


@pytest.mark.requirement("REVIEW-12")
def test_triage_prints_base_merge_pass_line_and_proof(
    repo: Path, reviewed: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a base-merge carry PASS omits either full SHA or the patch-id [then broken]."""
    from scripts import review_coverage

    _advance_main(repo, _OTHER, "other v2\n")
    head = _merge_main(repo)
    reviews = [_review_at(reviewed)]
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", repo)
    monkeypatch.setattr(review_coverage, "EXPECTED_REVIEWERS", (CLAUDE,))
    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: head)
    monkeypatch.setattr(review_coverage, "_changed_files", lambda pr: [_FILE, _OTHER])
    monkeypatch.setattr(review_coverage, "_checks", lambda pr: [])
    monkeypatch.setattr(
        review_coverage, "_paginated_json_list", lambda path: reviews if path.endswith("/reviews") else []
    )
    monkeypatch.setattr(review_coverage, "require_gate_current_with_main", lambda: "0" * 40)

    assert review_coverage.triage(_PR) == 0

    out = capsys.readouterr().out
    tip = _git(repo, "rev-parse", "origin/main")
    assert f"ok   {CLAUDE}: carried from {reviewed[:11]} (net diff unchanged since; base merge only)" in out
    assert f"base-merge carry: reviewed {reviewed} -> head {head}" in out
    assert net_diff(repo, tip, head).patch_id in out
    assert "[review-coverage] PASS" in out
