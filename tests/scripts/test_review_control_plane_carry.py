"""REVIEW-13 debt-only carry: two independent reviews survive a debt-file-only push.

Real git repositories in tmp_path (the same subprocess path production runs) and the
module's own fetch seam; no mocked verdicts. A control-plane PR authored -Claude is
reviewed by Grok and Cursor (the harnesses that need no Codex/Sol setup).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import review_control_plane, review_coverage, review_coverage_carry
from scripts.review_coverage_carry import debt_file_path
from scripts.review_gh import TriageError

_PR = "4626"
_DEBT = debt_file_path(_PR)
_LOGIN = "maintainer"
_UNFETCHABLE = "c" * 40
_CONTROL_PLANE_FILES = ["CLAUDE.md", _DEBT]

_GIT = review_coverage_carry._git()  # the production resolution, so tests and carry agree


def _git(root: Path, *args: str) -> str:
    return subprocess.run([_GIT, "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


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
    subprocess.run([_GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run([_GIT, "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "rename", "origin", "upstream")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "CLAUDE.md", "base\n")
    _git(work, "push", "-q", "origin", "HEAD:main")
    return work


def _review(lane: str, model: str, sha: str, *, commit_id: str | None = None) -> dict:
    return {
        "user": {"login": _LOGIN},
        "state": "COMMENTED",
        "body": f"<!-- {lane}-review v1 sha={sha} model={model} -->",
        "commit_id": sha if commit_id is None else commit_id,
    }


def _grok(sha: str) -> dict:
    return _review("grok", "grok-4.6", sha)


def _cursor(sha: str) -> dict:
    return _review("cursor", "composer-2.5", sha)


def _codex(sha: str, state: str) -> dict:
    """Codex is known by its bot login alone (no lane marker), so its state is the only gate."""
    return {
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "state": state,
        "body": "Codex Review: found two P2 issues.",
        "commit_id": sha,
    }


def _enforce(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    head: str,
    reviews: list[dict],
    *,
    author_trailer: str = "-Claude",
) -> tuple[int, str]:
    base = f"repos/{review_coverage.REPO}"
    pages = {
        f"{base}/pulls/{_PR}/commits": [
            {"sha": head, "parents": [{}], "commit": {"message": f"feat: x\n\n{author_trailer}"}}
        ],
        f"{base}/pulls/{_PR}/reviews": reviews,
        f"{base}/pulls/{_PR}/comments": [],
        f"{base}/issues/{_PR}/comments": [],
    }

    def captured(path: str) -> list[dict]:
        if path not in pages:
            raise AssertionError(f"unexpected read of {path}")
        return pages[path]

    monkeypatch.setattr(review_coverage, "_paginated_json_list", captured)
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: head)
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", repo)
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = review_control_plane.enforce(_PR, head, _CONTROL_PLANE_FILES)
    return rc, buf.getvalue()


# ----- (a) the carry --------------------------------------------------------


@pytest.mark.requirement("REVIEW-13")
def test_two_reviews_at_an_earlier_head_carry_over_a_debt_only_push(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] 2 reviews at R, debt-only since [then] pass naming both SHAs, [else stop]."""
    _commit(repo, "CLAUDE.md", "control plane edit\n")
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)])
    assert rc == 0, out
    assert "carried from" in out and "(debt-only since)" in out
    assert reviewed in out and head in out, "carried PASS line must name both full SHAs"
    assert _DEBT in out, "the diff path list is printed as proof"


@pytest.mark.requirement("REVIEW-13")
def test_one_carried_plus_one_at_head_from_another_family_counts_as_two(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] one review carried, another family at head [then] counts two, [else stop]."""
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(head)])
    assert rc == 0, out
    assert "Grok" in out and "Cursor" in out


@pytest.mark.requirement("REVIEW-13")
def test_a_submitted_codex_review_carries_like_a_lane_review(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Grok and a COMMENTED Codex review at R, debt-only since [then] pass, [else stop]."""
    # Control for the DISMISSED case below: Codex does carry, so that case fails on its state.
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(reviewed), _codex(reviewed, "COMMENTED")])
    assert rc == 0, out
    assert "Codex (carried from" in out


@pytest.mark.requirement("REVIEW-13")
@pytest.mark.parametrize("state", ["DISMISSED", "PENDING"])
def test_a_dismissed_or_pending_review_is_never_carried(
    repo: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """[if] Grok plus a DISMISSED or PENDING Codex review at R [then] FAIL, [else stop]."""
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(reviewed), _codex(reviewed, state)])
    assert rc == 1, out
    assert "Grok (carried from" in out
    assert "Codex" not in out.split("independent submitted reviews at head:", 1)[1].split("(need", 1)[0]


@pytest.mark.requirement("REVIEW-13")
def test_several_debt_only_commits_since_the_review_still_carry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] three debt-file-only commits since R [then] still carry, [else stop]."""
    # Overshoot control for the per-commit check: it must not reject a pure debt-only series.
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    _commit(repo, _DEBT, "debt v2\n")
    _commit(repo, _DEBT, "debt v3\n")
    head = _commit(repo, _DEBT, "debt v4\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)])
    assert rc == 0, out
    assert "carried from" in out


@pytest.mark.requirement("REVIEW-13")
def test_a_carried_review_cannot_be_the_authors_own_family(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] carried Grok on a -Grok PR [then] it does not count, [else stop]."""
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)], author_trailer="-Grok")
    assert rc == 1, out


# ----- (b) controls: no carry -----------------------------------------------


def _assert_fresh_reviews_needed(rc: int, out: str) -> None:
    assert rc == 1, out
    assert "FAIL: control-plane dual review (REVIEW-13)" in out
    assert "independent submitted reviews at head: (none)" in out
    assert "carried from" not in out


@pytest.mark.requirement("REVIEW-13")
def test_another_path_since_the_review_blocks_the_carry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] another path changed since review [then] no carry, [else stop]."""
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    _commit(repo, "CLAUDE.md", "changed after review\n")
    head = _commit(repo, _DEBT, "debt v2\n")
    _assert_fresh_reviews_needed(*_enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)]))


@pytest.mark.requirement("REVIEW-13")
def test_a_path_changed_then_restored_since_the_review_blocks_the_carry(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] CLAUDE.md changed then restored since R [then] no carry, [else stop]."""
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    _commit(repo, "CLAUDE.md", "changed after review\n")
    (repo / "CLAUDE.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "CLAUDE.md")
    head = _commit(repo, _DEBT, "debt v2\n")
    assert _git(repo, "diff", "--name-only", f"{reviewed}..{head}") == _DEBT, "net diff must be debt-only"
    _assert_fresh_reviews_needed(*_enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)]))


@pytest.mark.requirement("REVIEW-13")
def test_another_prs_debt_file_blocks_the_carry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] another PR's debt file changed [then] no carry, [else stop]."""
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    head = _commit(repo, debt_file_path("9999"), "other pr\n")
    _assert_fresh_reviews_needed(*_enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)]))


@pytest.mark.requirement("REVIEW-13")
def test_a_force_push_off_the_reviewed_head_blocks_the_carry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] reviewed SHA is not an ancestor [then] no carry, [else stop]."""
    base = _git(repo, "rev-parse", "HEAD")
    reviewed = _commit(repo, _DEBT, "debt v1\n")
    _git(repo, "checkout", "-q", base)
    head = _commit(repo, _DEBT, "rewritten history\n")
    _assert_fresh_reviews_needed(*_enforce(monkeypatch, repo, head, [_grok(reviewed), _cursor(reviewed)]))


@pytest.mark.requirement("REVIEW-13")
def test_an_unfetchable_reviewed_head_is_named_unknown_not_a_pass(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] reviewed SHA is unfetchable [then] FAIL as carry UNKNOWN, [else stop]."""
    head = _commit(repo, _DEBT, "debt v1\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(_UNFETCHABLE), _cursor(_UNFETCHABLE)])
    assert rc == 1, out
    assert "carry UNKNOWN" in out
    assert "carried from" not in out


@pytest.mark.requirement("REVIEW-13")
def test_a_review_of_the_head_itself_is_not_a_carry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] both reviews are at head [then] pass with no carry line, [else stop]."""
    head = _commit(repo, _DEBT, "debt v1\n")
    rc, out = _enforce(monkeypatch, repo, head, [_grok(head), _cursor(head)])
    assert rc == 0, out
    assert "carried from" not in out


# ----- (c) git by resolved path ----------------------------------------------


@pytest.fixture
def fresh_git_resolution() -> Iterator[None]:
    review_coverage_carry._git.cache_clear()
    yield
    review_coverage_carry._git.cache_clear()


@pytest.mark.requirement("REVIEW-13")
@pytest.mark.usefixtures("fresh_git_resolution")
def test_the_carry_launches_git_by_an_absolute_path() -> None:
    """[if] the carry resolves git [then] it is an absolute executable path, [else stop]."""
    git = review_coverage_carry._git()
    assert Path(git).is_absolute(), git
    assert os.access(git, os.X_OK), git


@pytest.mark.requirement("REVIEW-13")
@pytest.mark.usefixtures("fresh_git_resolution")
def test_no_git_on_path_is_an_error_not_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] git is not on PATH [then] TriageError, never a carry verdict, [else stop]."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(TriageError, match="git is not on PATH"):
        review_coverage_carry._git()
