"""Tests for destructive `git reset --hard` shell classification (issue #13260)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import git_safety

# ----- R-1 destructive hard reset -------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git reset --hard HEAD",
        "git -C /tmp/wt reset --hard origin/main",
        "bash -c 'git reset --hard'",
        "cd foo && git reset --hard",
    ],
)
def test_blocks_destructive_reset(command: str) -> None:
    """[if] a hard reset is requested [then] block, [else stop]."""
    assert git_safety.classify_shell_command(command) == "block"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git reset --hard", "block"),
        ("git reset --soft", "allow"),
    ],
)
def test_mutation_hard_to_soft_flips_verdict(command: str, expected: str) -> None:
    """One-token change between hard and soft must flip block/allow."""
    assert git_safety.classify_shell_command(command) == expected


# ----- R-2 read-only git mentioning reset text -----------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git log --grep='reset --hard'",
        'git commit -m "never git reset --hard"',
        "git grep 'reset --hard'",
        "git status",
        "git diff",
    ],
)
def test_allows_readonly_git_with_reset_text(command: str) -> None:
    """[if] a read-only git command mentions reset text [then] allow, [else stop]."""
    assert git_safety.classify_shell_command(command) == "allow"


# ----- R-3 safe reset-related help ------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git reset --help",
        "git reset -h",
        "git help reset",
        "git reset --soft HEAD~1",
        "git reset HEAD -- file.txt",
        "git reset HEAD -- --hard",
    ],
)
def test_allows_safe_reset_commands(command: str) -> None:
    """[if] a safe reset-related help command is invoked [then] allow, [else stop]."""
    assert git_safety.classify_shell_command(command) == "allow"


# ----- R-5 review bypass regressions ----------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git --git-dir=.git reset --hard",
        "git --work-tree=/wt reset --hard",
        "git status\ngit reset --hard",
        "bash -c 'git status'; git reset --hard",
        "bash -c 'git status' && git reset --hard",
        "git status & git reset --hard",
        "bash -lc 'git reset --hard'",
        "bash -ec 'git reset --hard'",
    ],
)
def test_blocks_review_bypasses(command: str) -> None:
    """[if] a hard reset is hidden by parser gaps [then] block, [else stop]."""
    assert git_safety.classify_shell_command(command) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "git status & git diff",
        "bash -lc 'git status'",
    ],
)
def test_allows_background_and_clustered_shell_without_hard_reset(command: str) -> None:
    """[if] background or -lc wraps only safe git [then] allow, [else stop]."""
    assert git_safety.classify_shell_command(command) == "allow"


# ----- R-4 unknown ------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git reset --ha",
        "echo 'unclosed",
    ],
)
def test_unknown_commands(command: str) -> None:
    assert git_safety.classify_shell_command(command) == "unknown"


def test_assert_safe_unknown_raises() -> None:
    with pytest.raises(git_safety.GitSafetyError, match="UNKNOWN"):
        git_safety.assert_safe_reset_command("git reset --ha")


def test_assert_safe_block_cites_policy() -> None:
    with pytest.raises(git_safety.GitSafetyError, match=r"git-safety\.md"):
        git_safety.assert_safe_reset_command("git reset --hard")


# ----- worktree label enrichment ----------------------------------------------------


@pytest.fixture
def origin_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "test"], check=True)
    (checkout / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(checkout), "branch", "-M", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "push", "--no-verify", "-q", "origin", "main"],
        check=True,
    )
    return origin, checkout


def test_block_message_names_primary_checkout(origin_repo: tuple[Path, Path]) -> None:
    _, checkout = origin_repo
    with pytest.raises(git_safety.GitSafetyError, match="primary checkout"):
        git_safety.assert_safe_reset_command("git reset --hard", repo=checkout)


def test_block_message_names_linked_worktree(origin_repo: tuple[Path, Path]) -> None:
    _, checkout = origin_repo
    linked = checkout.parent / "linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "worktree",
            "add",
            "-b",
            "linked-branch",
            str(linked),
        ],
        check=True,
    )
    with pytest.raises(git_safety.GitSafetyError, match="linked worktree"):
        git_safety.assert_safe_reset_command("git reset --hard", repo=linked)


# ----- CLI integration --------------------------------------------------------------


def test_cli_exit_codes() -> None:
    root = Path(__file__).resolve().parents[2]
    allow = subprocess.run(
        [sys.executable, "-m", "scripts.git_safety", "check", "--command", "git status"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    block = subprocess.run(
        [sys.executable, "-m", "scripts.git_safety", "check", "--command", "git reset --hard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    unknown = subprocess.run(
        [sys.executable, "-m", "scripts.git_safety", "check", "--command", "git reset --ha"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert allow.returncode == 0
    assert block.returncode == 1
    assert unknown.returncode == 2
