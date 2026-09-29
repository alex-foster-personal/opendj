"""Shared throwaway-repo builder for the scan_sast.sh end-to-end tests.

The positive-control contract is the TRACKED file list under the control dir, so the
throwaway repo copies only those files. A copytree of the live directory would carry a
stray untracked file (pytest's __pycache__/*.pyc beside the .py fixtures, Wed 16 Sep 2026)
into the throwaway repo, where `git add .` tracks it and scan_sast.sh then correctly
reports it as a control file semgrep did not scan.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL_DIR = "tests/fixtures/security/sast-control"
SCANNER_SCRIPTS = ("scan_sast.sh", "lib.sh", "secscan.py")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout


def tracked_control_files() -> list[str]:
    """The real repo's tracked control files, so a stray file in ROOT never leaks in."""
    files = git(ROOT, "ls-files", "--", CONTROL_DIR).splitlines()
    assert len(files) >= 4, f"expected the tracked control set under {CONTROL_DIR}, got {files}"
    return files


def init_scan_repo(tmp_path: Path) -> Path:
    """An uncommitted git repo holding the scanner scripts, custom rules and control files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "sast@test")
    git(repo, "config", "user.name", "sast test")
    shutil.copytree(ROOT / "tools" / "semgrep", repo / "tools" / "semgrep")
    for rel in tracked_control_files():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    sec_scripts = repo / "scripts" / "security"
    sec_scripts.mkdir(parents=True)
    for name in SCANNER_SCRIPTS:
        shutil.copy2(ROOT / "scripts" / "security" / name, sec_scripts / name)
    return repo
