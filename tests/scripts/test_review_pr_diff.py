"""The review tooling's diff source when GitHub refuses a PR for its size.

Every diff here comes out of a real `git diff` in a disposable repository. Only
the GitHub transport is replaced, and its refusal text is the one GitHub
returned for PR #3837 on Thu 1 Oct 2026.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import review_lane, review_pr_diff
from scripts.review_gh import TriageError

REPO = "example/repo"
SIZE_REFUSAL = (
    "gh api repos/example/repo/pulls/9 -H Accept: application/vnd.github.v3.diff failed (1): "
    "gh: Sorry, the diff exceeded the maximum number of files (300). Consider using "
    "'List pull requests files' API or locally cloning the repository instead. (HTTP 406)"
)
GATEWAY_FAILURE = "gh api repos/example/repo/pulls/9 failed (1): gh: Bad Gateway (HTTP 502)"


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def pr_repo(tmp_path: Path) -> dict[str, str | Path]:
    """base -> (main moves on) and base -> head: a PR whose merge base is not the base tip."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "kept.py").write_text("x = 1\nworkers = 2\n", encoding="utf-8")
    (tmp_path / "old_name.py").write_text("\n".join(f"line {i}" for i in range(30)) + "\n")
    _git(tmp_path, "add", "kept.py", "old_name.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    merge_base = _git(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "kept.py").write_text("x = 1\nworkers = 8\n", encoding="utf-8")
    _git(tmp_path, "mv", "old_name.py", "new_name.py")
    _git(tmp_path, "commit", "-q", "-am", "head")
    head = _git(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "main_only.py").write_text("main = True\n", encoding="utf-8")
    _git(tmp_path, "add", "main_only.py")
    _git(tmp_path, "commit", "-q", "-m", "main moved")
    base_tip = _git(tmp_path, "rev-parse", "HEAD")
    return {"root": tmp_path, "merge_base": merge_base, "head": head, "base_tip": base_tip}


def _github(monkeypatch: pytest.MonkeyPatch, pr_repo: dict, diff_failure: str) -> list[list[str]]:
    """Replace the GitHub transport; return the list of calls it received."""
    calls: list[list[str]] = []

    def gh(args: list[str], payload: dict | None = None, *, as_human: bool = False) -> str:
        calls.append(args)
        if args[0] == "api" and args[1].endswith("/pulls/9"):
            raise TriageError(diff_failure)
        if args[:2] == ["pr", "view"]:
            return json.dumps({"headRefOid": pr_repo["head"], "baseRefOid": pr_repo["base_tip"]})
        if args[0] == "api" and "/compare/" in args[1]:
            assert args[1].endswith(f"{pr_repo['base_tip']}...{pr_repo['head']}")
            return f"{pr_repo['merge_base']}\n"
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(review_pr_diff, "_gh", gh)
    return calls


def test_a_diff_github_refuses_for_size_is_read_from_the_checkout(
    monkeypatch: pytest.MonkeyPatch, pr_repo: dict
) -> None:
    """[if] the diff API answers HTTP 406 [then] the merge-base-to-head diff comes from git."""
    _github(monkeypatch, pr_repo, SIZE_REFUSAL)
    diff = review_pr_diff.pr_diff(REPO, "9", pr_repo["root"])
    assert "-workers = 2" in diff and "+workers = 8" in diff
    # Taken from the merge base, not the base tip: main's own later commit is
    # not part of the PR and must not appear as a deletion.
    assert "main_only.py" not in diff
    # GitHub's shape: a/ b/ headers and a detected rename, which the lane's
    # own parsers then read.
    assert "diff --git a/kept.py b/kept.py" in diff
    assert "rename from old_name.py" in diff and "rename to new_name.py" in diff
    assert review_lane.reviewed_paths_in_diff(diff) == {"kept.py", "new_name.py"}
    assert review_lane.anchorable_lines(diff)["kept.py"] == {1, 2}


def test_only_the_size_refusal_falls_back_to_the_checkout(monkeypatch: pytest.MonkeyPatch, pr_repo: dict) -> None:
    """[if] the diff API fails for any other reason [then] it raises and asks nothing else."""
    calls = _github(monkeypatch, pr_repo, GATEWAY_FAILURE)
    with pytest.raises(TriageError, match="HTTP 502"):
        review_pr_diff.pr_diff(REPO, "9", pr_repo["root"])
    assert len(calls) == 1


@pytest.mark.parametrize("missing", ["head", "merge_base"])
def test_a_commit_missing_from_the_checkout_is_a_measurement_failure(
    monkeypatch: pytest.MonkeyPatch, pr_repo: dict, missing: str
) -> None:
    """[if] the checkout lacks the head or the merge base [then] TriageError, never a diff."""
    pr_repo[missing] = "0" * 40
    _github(monkeypatch, pr_repo, SIZE_REFUSAL)
    with pytest.raises(TriageError, match="is not in the checkout"):
        review_pr_diff.pr_diff(REPO, "9", pr_repo["root"])


def test_the_callers_git_config_cannot_reshape_the_diff(monkeypatch: pytest.MonkeyPatch, pr_repo: dict) -> None:
    """[if] the checkout sets diff.noprefix and a color default [then] headers still parse."""
    _git(pr_repo["root"], "config", "diff.noprefix", "true")
    _git(pr_repo["root"], "config", "color.diff", "always")
    _git(pr_repo["root"], "config", "diff.renames", "false")
    _github(monkeypatch, pr_repo, SIZE_REFUSAL)
    diff = review_pr_diff.pr_diff(REPO, "9", pr_repo["root"])
    assert "\x1b[" not in diff
    assert review_lane.reviewed_paths_in_diff(diff) == {"kept.py", "new_name.py"}


def test_a_bare_carriage_return_in_a_changed_line_is_not_a_line_break(
    monkeypatch: pytest.MonkeyPatch, pr_repo: dict
) -> None:
    """[if] a changed line holds a bare CR [then] the diff keeps it inside that one line."""
    root = pr_repo["root"]
    _git(root, "checkout", "-q", "feature")
    (root / "kept.py").write_bytes(b"x = 1\nworkers = 8\ra = 2\n")
    _git(root, "commit", "-q", "-am", "carriage return")
    pr_repo["head"] = _git(root, "rev-parse", "HEAD")
    _github(monkeypatch, pr_repo, SIZE_REFUSAL)
    diff = review_pr_diff.pr_diff(REPO, "9", root)
    assert "+workers = 8\ra = 2\n" in diff


def test_review_lane_reads_its_diff_through_this_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] diff_of stops delegating here [then] triage and the lanes lose the fallback."""
    seen: list[tuple[str, str]] = []

    def source(repo: str, pr: str) -> str:
        seen.append((repo, pr))
        return "the diff"

    monkeypatch.setattr(review_lane, "pr_diff", source)
    assert review_lane.diff_of("9") == "the diff"
    assert seen == [(review_lane.REPO, "9")]
