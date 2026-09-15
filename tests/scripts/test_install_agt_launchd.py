"""Install script tests for AGT launchd agents.

[if] install_agt_launchd misses its seed or runs on Linux [then] installs on macOS, [else stop].
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO / "scripts" / "install_agt_launchd.sh"
_LAUNCHCTL_SHIM_LOG_ENV = "AF_AGT_LAUNCHCTL_SHIM_LOG"

pytestmark = pytest.mark.requirement("AGT-09")


def _launchctl_shim_dir(home: Path) -> tuple[Path, Path]:
    """Private launchctl that logs argv and exits 99; never touch live launchd."""
    bin_dir = home / ".local" / "bin" / "agt-test-shims"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = home / ".local" / "state" / "launchctl-shim.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "${_LAUNCHCTL_SHIM_LOG_ENV}"\n'
        "exit 99\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    return bin_dir, log_path


def _run_install(
    *args: str,
    home: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    shim_bin, log_path = _launchctl_shim_dir(home)
    merged = os.environ.copy()
    merged["HOME"] = str(home)
    merged["AF_AGT_STATE_DIR"] = str(home / ".local" / "state" / "af-agt")
    if env:
        merged.update(env)
    existing_path = merged.get("PATH", os.environ.get("PATH", ""))
    merged["PATH"] = f"{shim_bin}{os.pathsep}{existing_path}"
    merged[_LAUNCHCTL_SHIM_LOG_ENV] = str(log_path)
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


def _darwin_uname_shim(tmp_path: Path) -> Path:
    uname_dir = tmp_path / "uname-bin"
    uname_dir.mkdir()
    uname = uname_dir / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        'case "$1" in -s) echo Darwin ;; *) exec /usr/bin/uname "$@" ;; esac\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)
    return uname_dir


def test_render_to_produces_valid_plists(tmp_path: Path) -> None:
    """[if] install renders with seed [then] plists parse with correct intervals, [else stop]."""
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


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="Darwin host allows --install; Linux-only refusal is asserted elsewhere",
)
def test_install_darwin_only_on_linux(tmp_path: Path) -> None:
    """[if] --install runs on Linux [then] exit 2 Darwin-only, [else stop]."""
    home = tmp_path / "home"
    home.mkdir()
    _seed(home)
    proc = _run_install("--install", home=home)
    assert proc.returncode == 2
    assert "Darwin-only" in proc.stderr


def test_install_darwin_path_intercepts_launchctl_before_live_domain(tmp_path: Path) -> None:
    """Safety contract: Darwin --install branch hits the launchctl shim, not gui/<uid>."""
    home = tmp_path / "home"
    home.mkdir()
    _seed(home)
    uname_dir = _darwin_uname_shim(tmp_path)
    proc = _run_install(
        "--install",
        home=home,
        env={"PATH": f"{uname_dir}:/usr/bin:/bin"},
    )
    assert proc.returncode == 99, (proc.returncode, proc.stdout, proc.stderr)
    log_path = home / ".local" / "state" / "launchctl-shim.log"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    uid = os.getuid()
    plist = home / "Library" / "LaunchAgents" / "com.af.agt-personas.plist"
    assert lines == [
        f"bootout gui/{uid}/com.af.agt-personas",
        f"bootstrap gui/{uid} {plist}",
    ]
