"""Real-git history integrity tests for the Trunk merge-path audit.

Requirements (DEVOPS-4215):
- [if] local history is partial or rewritten [then] the audit is UNKNOWN, [else stop].
- [if] first-parent records contain line separators [then] records stay exact, [else stop].
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import trunk_merge_path_audit as mod
from tests.scripts.test_trunk_merge_path_audit import HUMAN, _commit, _git, _init_repo

pytestmark = pytest.mark.requirement("DEVOPS-4215")


def test_shallow_clone_is_unknown_but_full_clone_is_readable(tmp_path: Path) -> None:
    """[if] a clone is shallow [then] refuse it while a full clone reads, [else stop]."""
    source, root = _init_repo(tmp_path)
    tip = _commit(source, "tip.txt", "tip")
    shallow = tmp_path / "shallow"
    full = tmp_path / "full"
    _git(source, "clone", "-q", "--depth", "1", source.as_uri(), str(shallow))
    _git(source, "clone", "-q", source.as_uri(), str(full))
    with pytest.raises(mod.Unknown, match="shallow clone"):
        mod.first_parent_commits(shallow, ["main"])
    assert [row.sha for row in mod.range_commits(full, root, tip)] == [tip]


def test_grafted_repository_is_unknown(tmp_path: Path) -> None:
    """[if] info/grafts exists [then] refuse rewritten history, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    tip = _commit(repo, "tip.txt", "tip")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "grafts").write_text(f"{tip}\n", encoding="utf-8")
    with pytest.raises(mod.Unknown, match="grafts rewrite"):
        mod.first_parent_commits(repo, ["main"])


def test_replace_object_cannot_change_commit_identity(tmp_path: Path) -> None:
    """[if] refs/replace forges an author [then] the audit reads the original, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    tip = _commit(repo, "tip.txt", "tip", who=HUMAN)
    tree = _git(repo, "rev-parse", f"{tip}^{{tree}}")
    parent = _git(repo, "rev-parse", f"{tip}^1")
    forged = _git(
        repo,
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        "forged",
        who=("bot", mod.TRUNK_BOT_EMAIL),
    )
    _git(repo, "replace", tip, forged)
    [row] = mod.range_commits(repo, parent, tip)
    assert row.author_email == HUMAN[1]


def test_second_parent_range_base_is_unknown(tmp_path: Path) -> None:
    """[if] GOOD is only a second-parent ancestor [then] reject the range, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "topic")
    topic = _commit(repo, "topic.txt", "topic")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main.txt", "main")
    _git(repo, "merge", "-q", "--no-ff", "topic", "-m", "merge")
    merge = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(mod.Unknown, match="first-parent chain"):
        mod.range_commits(repo, topic, merge)


def test_nonancestor_range_is_unknown(tmp_path: Path) -> None:
    """[if] GOOD is not an ancestor of BAD [then] reject the range, [else stop]."""
    repo, root = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "other")
    other = _commit(repo, "other.txt", "other")
    _git(repo, "checkout", "-q", "main")
    tip = _commit(repo, "tip.txt", "tip")
    assert root != other
    with pytest.raises(mod.Unknown, match="not an ancestor"):
        mod.range_commits(repo, other, tip)


def test_cutover_partitions_legacy_and_current_history(tmp_path: Path) -> None:
    """[if] history straddles cutover [then] only older SHAs are legacy, [else stop]."""
    repo, root = _init_repo(tmp_path)
    legacy = _commit(repo, "legacy.txt", "legacy", when="2026-09-30T00:00:00Z")
    cutover = _commit(repo, "cutover.txt", "cutover", when="2026-10-01T00:00:00Z")
    current = _commit(repo, "current.txt", "current", when="2026-10-02T00:00:00Z")
    commits = mod.range_commits(repo, root, current)
    assert mod.legacy_shas(repo, commits, cutover) == frozenset({legacy})


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_git_start_or_timeout_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch, failure: str, tmp_path: Path
) -> None:
    """[if] git cannot start or times out [then] report UNKNOWN, [else stop]."""
    if failure == "missing":
        monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    else:
        def fail(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(["git"], mod.GIT_TIMEOUT_SECONDS)
        monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(mod.Unknown):
        mod.first_parent_commits(tmp_path, ["main"])


@pytest.mark.parametrize("separator", ["\r", "\u2028"])
def test_subject_line_separator_stays_inside_one_record(tmp_path: Path, separator: str) -> None:
    """[if] a subject contains a line separator [then] NUL framing preserves it, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    subject = f"odd{separator}subject"
    sha = _commit(repo, "odd.txt", subject)
    [row] = mod.range_commits(repo, f"{sha}^", sha)
    assert row.subject == subject


def test_clock_skew_does_not_hide_later_history(tmp_path: Path) -> None:
    """[if] commit time regresses [then] since-as-filter reaches later rows, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    first = _commit(repo, "first.txt", "first", when="2026-10-02T10:00:00Z")
    old = _commit(repo, "old.txt", "old", when="2026-09-01T00:00:00Z")
    tip = _commit(repo, "tip.txt", "tip", when="2026-10-02T12:00:00Z")
    rows = mod.window_commits(repo, "main", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z")
    assert [row.sha for row in rows] == [tip, first]
    assert old not in {row.sha for row in rows}


def test_unsupported_since_as_filter_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] git rejects since-as-filter [then] window selection is UNKNOWN, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    real_git = mod._git

    def reject_filter(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "log" and any(arg.startswith("--since-as-filter=") for arg in args):
            return subprocess.CompletedProcess(args, 129, "", "unknown option")
        return real_git(repo_path, *args)

    monkeypatch.setattr(mod, "_git", reject_filter)
    with pytest.raises(mod.Unknown, match="git log"):
        mod.window_commits(repo, "main", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z")
