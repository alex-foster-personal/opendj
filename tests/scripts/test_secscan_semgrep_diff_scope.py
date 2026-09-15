"""secscan semgrep-diff-scope: PR SKIP comes from git diff scope, not semgrep 0/0."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECSCAN = ROOT / "scripts" / "security" / "secscan.py"
SEMGREP = ROOT / ".tmp" / "security" / "bin" / "semgrep"
CONFIGS = [
    "p/python",
    "p/typescript",
    "p/rust",
    str(ROOT / "tools" / "semgrep"),
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "scope@test")
    _git(repo, "config", "user.name", "scope test")


def _scope(
    repo: Path,
    base: str,
    head: str = "HEAD",
    *,
    semgrep: str | None = None,
    configs: list[str] | None = None,
) -> tuple[int, str]:
    cfg_list = configs or CONFIGS
    cmd = [
        sys.executable,
        str(SECSCAN),
        "semgrep-diff-scope",
        "--root",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--semgrep",
        semgrep or str(SEMGREP),
        *sum([["--config", cfg] for cfg in cfg_list], []),
        "--exclude",
        "tests/fixtures/security",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip()


@pytest.mark.skipif(not SEMGREP.exists(), reason="semgrep not installed")
def test_docs_only_diff_counts_zero(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    shutil.copytree(ROOT / "tools" / "semgrep", repo / "tools" / "semgrep")
    (repo / "README.md").write_text("# one\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "README.md").write_text("# two\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "docs")
    rc, count = _scope(
        repo,
        base,
        configs=[
            "p/python",
            "p/typescript",
            "p/rust",
            str(repo / "tools" / "semgrep"),
        ],
    )
    assert rc == 0
    assert count == "0"


def test_exclude_prefix_counts_zero_without_semgrep(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sec_dir = repo / "tests/fixtures/security"
    sec_dir.mkdir(parents=True)
    (sec_dir / "probe.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (sec_dir / "probe.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "tests/fixtures/security/probe.py")
    _git(repo, "commit", "-m", "fixture")
    rc, count = _scope(repo, base, semgrep="/bin/false")
    assert rc == 0
    assert count == "0"


def test_deleted_only_diff_counts_zero_without_semgrep(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "apps").mkdir()
    (repo / "apps/old.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "apps/old.py")
    _git(repo, "commit", "-m", "init")
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "apps/old.py").unlink()
    _git(repo, "add", "apps/old.py")
    _git(repo, "commit", "-m", "delete")
    rc, count = _scope(repo, base, semgrep="/bin/false")
    assert rc == 0
    assert count == "0"


@pytest.mark.skipif(not SEMGREP.exists(), reason="semgrep not installed")
def test_python_diff_counts_positive_with_semgrep(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    shutil.copytree(ROOT / "tools" / "semgrep", repo / "tools" / "semgrep")
    (repo / "apps").mkdir()
    (repo / "apps/foo.py").write_text("import subprocess\nsubprocess.call('ls', shell=True)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "apps/foo.py").write_text("import subprocess\nsubprocess.call('pwd', shell=True)\n", encoding="utf-8")
    _git(repo, "add", "apps/foo.py")
    _git(repo, "commit", "-m", "python")
    rc, count = _scope(
        repo,
        base,
        configs=[
            "p/python",
            "p/typescript",
            "p/rust",
            str(repo / "tools" / "semgrep"),
        ],
    )
    assert rc == 0
    assert int(count) > 0
