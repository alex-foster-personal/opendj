"""scripts/ci_apt_install.sh: the one way CI installs apt packages as root (fleet-af #742).

Regression lines are the script's own: on a fleet host it calls only the scoped helper, it refuses
no-argument and option-shaped calls before any sudo, and elsewhere it runs apt-get from /.
Both branches run here with a recording `sudo` stand-in, unprivileged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_apt_install.sh"
REAL_HELPER = "/usr/local/sbin/ci-apt-install"


def _run(tmp_path: Path, helper: str, *packages: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run a copy of the script with HELPER pointed at `helper` and a sudo that records its argv."""
    text = SCRIPT.read_text()
    assert text.count(f"HELPER={REAL_HELPER}\n") == 1, "the helper path moved; update this test"
    script = tmp_path / "ci_apt_install.sh"
    script.write_text(text.replace(f"HELPER={REAL_HELPER}\n", f"HELPER={helper}\n"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sudo.argv"
    sudo = bin_dir / "sudo"
    sudo.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >"{log}"\n')
    sudo.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(script), *packages],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, (log.read_text().splitlines() if log.exists() else [])


def test_with_the_helper_installed_only_the_helper_runs_as_root(tmp_path: Path) -> None:
    helper = tmp_path / "ci-apt-install"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    proc, argv = _run(tmp_path, str(helper), "ffmpeg", "file")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-n", str(helper), "ffmpeg", "file"], (
        f"if the helper is installed and this runs apt-get some other way then broken: {argv}"
    )


def test_without_the_helper_apt_runs_from_root_dir(tmp_path: Path) -> None:
    proc, argv = _run(tmp_path, str(tmp_path / "absent"), "ffmpeg")
    assert proc.returncode == 0, proc.stderr
    assert argv[:4] == ["env", "-C", "/", "sh"], f"if raw apt holds the workspace as cwd then broken: {argv}"
    assert argv[-1] == "ffmpeg" and "apt-get install -y" in argv[5], argv


@pytest.mark.parametrize("packages", [(), ("-o", "APT::Update::Pre-Invoke::=/bin/sh"), ("ffmpeg", "--reinstall")])
def test_refuses_no_package_or_an_option_before_any_sudo(tmp_path: Path, packages: tuple[str, ...]) -> None:
    proc, argv = _run(tmp_path, str(tmp_path / "absent"), *packages)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert argv == [], f"if a refused call still reaches sudo then broken: {argv}"
