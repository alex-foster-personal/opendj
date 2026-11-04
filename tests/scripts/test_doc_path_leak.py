"""scripts/doc_path_leak.py: a real home path on an added doc line fails before review.

- if an added specs/ line carries a lane worktree home path then it is a finding
- if an added app_docs/ line carries a macOS home path then it is a finding
- if the home path sits inside a `bash` fence then it is still a finding (not a transcript)
- if the line also carries <worktree> then it passes
- if the line sits inside a `console` or `log` fence then it passes
- if the login is a repo placeholder (dev) then it passes
- if the match is a URL route (example.com/home/user) then it passes
- if the leak is on a context (unchanged) line then it is not reported
- if the leak is in a non-doc file (scripts/) then it is not reported
- if the CLI runs the control through git then it exits 1 and prints file:line and the fix
- if the --control self-test finds exactly the planted leaks then it exits 0
- if --report-md is given with findings then the markdown section is appended
- if a doc path is non-ASCII (git would quote it) then its leak is still reported
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.doc_path_leak import find_leaks, transcript_fence_lines

REPO = Path(__file__).resolve().parents[2]
CONTROL = REPO / "tests" / "fixtures" / "security" / "doc-path-leak-control"
EXPECTED = [("(private path)", 2), ("(private path)", 3), ("(private path)", 9)]


def _login() -> str:
    # Minted per run so no tracked file names a home directory.
    return "zq" + secrets.token_hex(4)


def _render(text: str, login: str) -> str:
    return text.replace("@HOME@", "/" + "home/" + login).replace("@MACHOME@", "/" + "Users/" + login)


def _materialize(tmp_path: Path) -> tuple[Path, Path, str]:
    login = _login()
    head = tmp_path / "head"
    for src in (CONTROL / "head").rglob("*.md"):
        dest = head / src.relative_to(CONTROL / "head")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_render(src.read_text(), login))
    diff = tmp_path / "control.diff"
    diff.write_text(_render((CONTROL / "control.diff").read_text(), login))
    return diff, head, login


def test_control_diff_yields_exactly_the_three_planted_leaks(tmp_path: Path) -> None:
    diff, head, login = _materialize(tmp_path)
    leaks = find_leaks(diff.read_text(), lambda path: (head / path).read_text())
    assert sorted((leak.path, leak.line) for leak in leaks) == EXPECTED, (
        "if doc_path_leak misses a planted leak or flags an exempt line then broken"
    )
    assert {leak.login for leak in leaks} == {login}


def test_transcript_fences_cover_console_and_log_only() -> None:
    content = "a\n```console\nx\n```\n```log\ny\n```\n```bash\nz\n```\n"
    assert transcript_fence_lines(content) == {3, 6}, (
        "if a bash fence counts as a transcript, or a console/log body is missed, then broken"
    )


def test_cli_through_git_fails_with_file_line_and_fix(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git, "git is required"
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }

    def run(*args: str) -> str:
        return subprocess.run([git, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout

    run("init", "-q")
    (repo / "specs").mkdir()
    (repo / "specs" / "plan.md").write_text("# Plan\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").strip()
    login = _login()
    (repo / "specs" / "plan.md").write_text(
        "# Plan\nWorktree: " + _render("@HOME@", login) + "/code/wt/\nOK: <worktree>/scripts/x.py\n"
    )
    run("commit", "-qam", "head")
    head = run("rev-parse", "HEAD").strip()

    result = subprocess.run(
        [sys.executable, "-m", "scripts.doc_path_leak", "--root", str(repo), "--base", base, "--head", head],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "(private path):2: real home path" in result.stdout
    assert "use a repo-relative path" in result.stdout
    assert "doc-path-leak: 1 finding(s)" in result.stdout


def test_cli_without_inputs_or_with_a_bad_ref_is_not_clean(tmp_path: Path) -> None:
    bad = subprocess.run(
        [sys.executable, "-m", "scripts.doc_path_leak", "--root", str(tmp_path), "--base", "nope", "--head", "nope"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2, "if an unreadable diff reads as clean (exit 0) then broken"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.doc_path_leak", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def test_control_self_test_exits_zero_on_the_planted_leaks() -> None:
    result = _cli("--control", str(CONTROL))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fired(3) of 3 planted" in result.stdout, "if the control self-test misreports its count then broken"


def test_report_md_and_non_ascii_doc_path_are_reported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
        ).stdout

    run("init", "-q")
    (repo / "specs").mkdir()
    (repo / "specs" / "base.md").write_text("# Base\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").strip()
    (repo / "specs" / "caf\u00e9.md").write_text("Worktree: " + _render("@HOME@", _login()) + "/code/wt/\n")
    run("add", "-A")
    run("commit", "-qm", "head")
    head = run("rev-parse", "HEAD").strip()
    report = tmp_path / "report.md"
    result = _cli("--root", str(repo), "--base", base, "--head", head, "--report-md", str(report))
    assert result.returncode == 1, "if a git-quoted (non-ASCII) doc path escapes the scan then broken"
    assert "(private path)\u00e9.md:1: real home path" in result.stdout
    assert "### doc-path-leak (1)" in report.read_text(), "if --report-md does not append the section then broken"
