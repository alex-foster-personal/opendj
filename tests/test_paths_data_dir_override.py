"""MDT_DATA_DIR must reach apps.shared.paths.DATA_DIR (worktree data access).

Regression line:
  - if MDT_DATA_DIR is set and paths.DATA_DIR ignores it then broken
    (worktrees silently read their own empty data/ instead of the primary's)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _data_dir_with_env(value: str | None) -> str:
    code = "from apps.shared import paths; print(paths.DATA_DIR)"
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)}
    if value is not None:
        env["MDT_DATA_DIR"] = value
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, check=True,
    ).stdout.strip()


def test_env_override_wins(tmp_path):
    assert _data_dir_with_env(str(tmp_path)) == str(tmp_path)


def test_default_is_project_data():
    out = _data_dir_with_env(None)
    assert out.endswith("/data")
