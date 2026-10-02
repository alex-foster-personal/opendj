"""quality_gate's Python file walk skips gitignored node_modules installs.

CI keeps node_modules between jobs (scripts/ci_clean_untracked.sh excludes it
from `git clean`), and node-gyp ships Python. On a runner that once ran the
electron job, the ratchet measured node-gyp's msvs.py as the repo's longest
Python file (3970 lines, agentbox-9, Fri 2 Oct 2026) and failed a PR for it.

Regression lines:
  - if a .py under any node_modules directory is walked, then broken
  - if a first-party .py beside it is skipped, then broken (overshoot control)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import quality_gate


def test_python_files_skip_node_modules_but_keep_first_party(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours = tmp_path / "apps" / "desktop" / "electron" / "ours.py"
    theirs = tmp_path / "apps" / "desktop" / "electron" / "node_modules" / ".pnpm" / "gyp" / "msvs.py"
    for path in (ours, theirs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(quality_gate, "REPO", tmp_path)
    monkeypatch.setattr(quality_gate.CFG, "PY_PATHS", ("apps",))

    found = quality_gate._python_files()

    assert ours in found
    assert theirs not in found
