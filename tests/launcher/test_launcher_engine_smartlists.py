"""[if] launcher smartlist HTTP ships [then] cargo engine tests pass, [else stop]."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("SMART-12074")

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_DIR = REPO_ROOT / "apps" / "launcher" / "src-tauri"


def _cargo_engine_tests() -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "launcher",
            "commands::engine::tests",
            "--",
            "--nocapture",
        ],
        cwd=TAURI_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_launcher_engine_origin_and_smartlist_cargo_acceptance():
    """[if] disposable engine tests run [then] all engine::tests pass, [else stop]."""
    proc = _cargo_engine_tests()
    assert proc.returncode == 0, proc.stderr[-4000:] if proc.stderr else proc.stdout
