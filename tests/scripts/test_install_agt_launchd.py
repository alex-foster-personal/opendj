"""Install script tests for AGT launchd agents."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO / "scripts" / "install_agt_launchd.sh"

pytestmark = pytest.mark.requirement("AGT-09")


def _run_install(
    *args: str,
    home: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["HOME"] = str(home)
    merged["AF_AGT_STATE_DIR"] = str(home / ".local" / "state" / "af-agt")
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        cwd=str(REPO),
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(home: Path) -> None:
    seed = home / ".local" / "state" / "af-agt" / "seed"
    seed.mkdir(parents=True)
    (seed / "state.db").write_bytes(b"s")
    (seed / "master.plain.db").write_bytes(b"m")


def test_render_to_produces_valid_plists(tmp_path: Path) -> None:
    """[if] install renders with seed present [then] plists parse with correct intervals, [else stop]."""
    home = tmp_path / "home"
    home.mkdir()
    _seed(home)
    out = tmp_path / "plists"
    proc = _run_install("--render-to", str(out), home=home)
    assert proc.returncode == 0, proc.stderr
    personas = plistlib.loads((out / "com.af.agt-personas.plist").read_bytes())
    soak = plistlib.loads((out / "com.af.agt-soak.plist").read_bytes())
    assert personas["StartInterval"] == 7200
    assert soak["StartCalendarInterval"]["Hour"] == 3
    assert soak["StartCalendarInterval"]["Minute"] == 20
    path = personas["EnvironmentVariables"]["PATH"]
    assert ".local/bin" in path
    args = personas["ProgramArguments"]
    assert "ops.agentic_testing.scheduler" in args
    assert "personas" in args


def test_missing_seed_exits_two(tmp_path: Path) -> None:
    """[if] seed files are missing [then] installer exits 2, [else stop]."""
    home = tmp_path / "home"
    home.mkdir()
    proc = _run_install("--render-to", str(tmp_path / "out"), home=home)
    assert proc.returncode == 2
    assert "seed/state.db" in proc.stderr


def test_forbidden_path_refused(tmp_path: Path) -> None:
    """[if] HOME resolves under Open DJ.app [then] installer exits 2, [else stop]."""
    home = tmp_path / "Applications" / "Open DJ.app" / "Contents"
    home.mkdir(parents=True)
    _seed(home)
    proc = _run_install("--render-to", str(tmp_path / "out"), home=home)
    assert proc.returncode == 2
    assert "forbidden path marker" in proc.stderr


def test_install_darwin_only_on_linux(tmp_path: Path) -> None:
    """[if] --install runs on Linux [then] exit 2 Darwin-only, [else stop]."""
    home = tmp_path / "home"
    home.mkdir()
    _seed(home)
    proc = _run_install("--install", home=home)
    if sys.platform == "darwin":
        pytest.skip("Darwin host allows --install")
    assert proc.returncode == 2
    assert "Darwin-only" in proc.stderr
