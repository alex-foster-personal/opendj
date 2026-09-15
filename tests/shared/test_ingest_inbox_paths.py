"""INGEST_INBOX must honor MDT_DATA_DIR at import time (issue #3071).

Regression lines:
  - if MDT_DATA_DIR is set at import then INGEST_INBOX is under that data dir
  - if MDT_DATA_DIR is unset then INGEST_INBOX stays under HOME/Music/...
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _ingest_inbox_with_env(
    *,
    data_dir: str | None,
    home: str,
) -> str:
    code = "from apps.shared.paths import INGEST_INBOX; print(INGEST_INBOX)"
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO),
        "HOME": home,
    }
    if data_dir is not None:
        env["MDT_DATA_DIR"] = data_dir
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout.strip()


def test_ingest_inbox_under_data_dir_when_mdt_data_dir_set(tmp_path):
    sandbox = tmp_path / "sandbox-data"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    out = _ingest_inbox_with_env(data_dir=str(sandbox), home=str(fake_home))
    assert out == str(sandbox / "Manual Library" / "_ingest")


def test_ingest_inbox_under_home_music_when_mdt_data_dir_unset(tmp_path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    out = _ingest_inbox_with_env(data_dir=None, home=str(fake_home))
    assert out == str(fake_home / "Music" / "Manual Library" / "_ingest")
