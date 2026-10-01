"""REVIEW-13 debt-only carry: two independent reviews survive a debt-file-only push.

Real git repositories in tmp_path (the same subprocess path production runs) and the
module's own fetch seam; no mocked verdicts. A control-plane PR authored -Claude is
reviewed by Grok and Cursor (the harnesses that need no Codex/Sol setup).
"""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import review_control_plane, review_coverage
from scripts.review_coverage_carry import debt_file_path

_PR = "4626"
_DEBT = debt_file_path(_PR)
_LOGIN = "maintainer"
_UNFETCHABLE = "c" * 40
_CONTROL_PLANE_FILES = ["CLAUDE.md", _DEBT]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


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
