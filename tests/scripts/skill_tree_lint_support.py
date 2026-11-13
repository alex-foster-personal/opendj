"""Shared fixtures for skill_tree_lint tests.

Real git-backed tmp_path repos and skill directory builders used by every
sibling module under ``tests/scripts/test_skill_tree_lint*.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import SplitResult

import pytest

from scripts import skill_tree_lint
from scripts.skill_tree_lint import (
    CFG,
    UnparsableDestinationError,
    discover,
    file_ref_depth,
    main,
    measure,
    skill_graph,
)

REPO = Path(__file__).resolve().parents[2]


def _skill(repo: Path, name: str, body: str = "", tree: str = "(private path)") -> Path:
    root = repo / tree / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return root


def _git(repo: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None, "git is required: the scan reads its expected skill set from the index"
    subprocess.run([git, "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, fillers: int = CFG.MIN_SKILLS) -> Path:
    # A real checkout: measure() reads the tracked SKILL.md set from git's index.
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    (tmp_path / CFG.RULES_DIR).mkdir(parents=True, exist_ok=True)
    for i in range(fillers):
        _skill(tmp_path, f"filler-skill-{i:02d}", "Plain instructions, no references.")
    return tmp_path
