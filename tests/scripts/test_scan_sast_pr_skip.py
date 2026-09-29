"""scan_sast.sh pr mode SKIP comes from diff scope before the diff-aware scan."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.scripts.sast_control_fixtures import SEMGREP_BIN, init_scan_repo, needs_scanners


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _bin_dir(work: Path) -> Path:
    bin_dir = work / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("semgrep", "uv"):
        dest = bin_dir / tool
        if not dest.exists():
            os.symlink(SEMGREP_BIN / tool, dest)
    return bin_dir


def _run_pr_scan(repo: Path, work: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    _bin_dir(work)
    env = os.environ.copy()
    env["SECURITY_WORK_DIR"] = str(work)
    env["SECURITY_BASE_SHA"] = base
    env["SECURITY_HEAD_SHA"] = head
    return subprocess.run(
        ["bash", "scripts/security/scan_sast.sh", "pr"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_pair(repo: Path, rel: str, first: str, second: str) -> tuple[str, str]:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(first, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / rel).write_text(second, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", "change")
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return base, head


@needs_scanners
def test_docs_only_pr_writes_skip_row_via_scan_sast(tmp_path: Path) -> None:
    repo = init_scan_repo(tmp_path)
    base, head = _commit_pair(repo, "docs/foo.md", "# one\n", "# two\n")
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work, base, head)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    summary = (work / "summary.tsv").read_text(encoding="utf-8")
    assert "semgrep\tpr\t" in summary
    assert "\tSKIP\t" in summary
    assert "no scannable file changed" in summary


@needs_scanners
def test_tests_only_pr_writes_skip_row_via_scan_sast(tmp_path: Path) -> None:
    """[if] the PR changes only a tests/ file [then] SKIP, never UNKNOWN, [else stop].

    End to end over the shape that failed as UNKNOWN in job 104882711482 on PR #3362.
    """
    repo = init_scan_repo(tmp_path)
    body = "import subprocess\nsubprocess.call({!r}, shell=True)\n"
    base, head = _commit_pair(
        repo, "tests/scripts/test_probe.py", body.format("ls"), body.format("pwd")
    )
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work, base, head)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    summary = (work / "summary.tsv").read_text(encoding="utf-8")
    assert "\tSKIP\t" in summary, summary
    assert "UNKNOWN" not in summary, summary
    assert "no scannable file changed" in summary


@needs_scanners
def test_scannable_pr_still_runs_the_scan(tmp_path: Path) -> None:
    """[if] the PR changes a file outside the ignore list [then] a real scan row, [else stop].

    Negative control for the two SKIP cases: the same wrapper must still reach a verdict
    when there is something to scan, so SKIP cannot be the answer to everything.
    """
    repo = init_scan_repo(tmp_path)
    body = "import subprocess\nsubprocess.call({!r}, shell=True)\n"
    base, head = _commit_pair(repo, "apps/probe.py", body.format("ls"), body.format("pwd"))
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work, base, head)
    summary = (work / "summary.tsv").read_text(encoding="utf-8")
    assert "\tSKIP\t" not in summary, summary
    assert "UNKNOWN" not in summary, summary
    assert proc.returncode in (0, 1), proc.stderr or proc.stdout
