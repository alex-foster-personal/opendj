"""scripts/ci_apt_present.sh lets a warm runner skip apt and the packages lock.

Every workflow site that installs packages under the host-wide `packages` lock
must first ask this script whether the same packages are already installed, so
a burst of shards on a warm host no longer queues for an index refresh.

Regression lines:
  - if every package is installed and the script exits nonzero then warm hosts
    queue on the packages lock again
  - if a package is absent, half-configured or removed and the script exits 0
    then a cold host skips its install and the suite dies on a missing binary
  - if the script runs with no package then it must fail with usage, never
    report "all installed"
  - if a host has no dpkg-query then it must install, not skip
  - if a workflow installs under the packages lock without the presence check,
    or checks a different package list than it installs, then broken
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_apt_present.sh"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

FAKE_DPKG_QUERY = """#!/usr/bin/env bash
# fake dpkg-query -W -f='${Status}' <pkg>: statuses come from $FAKE_DPKG_DB
pkg="${@: -1}"
if ! awk -F '\\t' -v p="$pkg" '$1 == p { printf "%s", $2; found = 1 } END { exit !found }' "$FAKE_DPKG_DB"; then
    echo "dpkg-query: no packages found matching $pkg" >&2
    exit 1
fi
"""


def _fake_bin(tmp_path: Path, statuses: dict[str, str]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "dpkg-query"
    fake.write_text(FAKE_DPKG_QUERY)
    fake.chmod(0o755)
    db = tmp_path / "dpkg.db"
    db.write_text("".join(f"{pkg}\t{status}\n" for pkg, status in statuses.items()))
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DPKG_DB": str(db)}


def _run(*pkgs: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *pkgs], env=env, capture_output=True, text=True, timeout=30, check=False
    )


# ----------------------------------------------------------------------------
# the script against a fake dpkg database


def test_all_installed_skips_apt(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, {"a": "install ok installed", "b": "install ok installed"})
    proc = _run("a", "b", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all 2 packages installed" in proc.stdout


@pytest.mark.parametrize(
    ("status_b", "why"),
    [
        (None, "unknown to dpkg"),
        ("install ok half-configured", "half-configured"),
        ("deinstall ok config-files", "removed, config left"),
        ("install ok unpacked", "unpacked, not configured"),
    ],
)
def test_any_package_not_fully_installed_means_install(
    tmp_path: Path, status_b: str | None, why: str
) -> None:
    statuses = {"a": "install ok installed"}
    if status_b is not None:
        statuses["b"] = status_b
    proc = _run("a", "b", env=_fake_bin(tmp_path, statuses))
    assert proc.returncode == 1, f"{why}: {proc.stdout}{proc.stderr}"
    assert "missing: b" in proc.stdout, why


def test_no_package_is_a_usage_error(tmp_path: Path) -> None:
    proc = _run(env=_fake_bin(tmp_path, {}))
    assert proc.returncode == 2
    assert "usage" in proc.stderr


def test_no_dpkg_query_installs_rather_than_skips(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bash = shutil.which("bash")
    assert bash, "test needs bash"
    (bin_dir / "bash").symlink_to(bash)
    proc = _run("a", env={**os.environ, "PATH": str(bin_dir)})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "cannot measure" in proc.stdout


# ----------------------------------------------------------------------------
# the script against the real instrument: a control in each direction


@pytest.mark.skipif(shutil.which("dpkg-query") is None, reason="needs a dpkg host")
def test_real_dpkg_reports_an_installed_package_present_and_a_bogus_one_missing() -> None:
    env = dict(os.environ)
    assert _run("bash", env=env).returncode == 0
    proc = _run("bash", "mdt-no-such-package-for-this-test", env=env)
    assert proc.returncode == 1
    assert "missing: mdt-no-such-package-for-this-test" in proc.stdout


# ----------------------------------------------------------------------------
# every locked install site in the workflows is fronted by the check

LOCKED_INSTALL = re.compile(r"apt-get install -y ([a-z0-9][a-z0-9 .+-]*?)'?$")
PRESENCE_PREFIX = re.compile(
    r"^scripts/ci_apt_present\.sh ([a-z0-9 .+-]+?) \|\| scripts/ci_host_lock\.sh packages "
)
# A package standing in for a capability is fronted by that capability's own
# probe, on the line before the install, instead of by a package check: any
# LLVM's libclang serves bindgen, so libclang1 installs only when none loads
# (the rest of that step is pinned by tests/scripts/test_ci_libclang_present.py).
CAPABILITY_PROBES = {"libclang1": "python -m scripts.ci_libclang_present || {"}


def _locked_install_sites() -> list[tuple[str, str, str]]:
    """(site, line, the line before it) for every install under the packages lock."""
    return [
        (f"{path.name}:{lineno}", line.strip(), lines[lineno - 2].strip())
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        for lines in [path.read_text().splitlines()]
        for lineno, line in enumerate(lines, 1)
        if "ci_host_lock.sh packages sudo" in line and "apt-get install -y" in line
    ]


def test_locked_install_sites_exist_to_be_checked() -> None:
    """A selector matching nothing proves nothing."""
    assert len(_locked_install_sites()) >= 8


def test_every_locked_install_checks_the_same_packages_first() -> None:
    offenders = []
    for site, line, before in _locked_install_sites():
        installed = LOCKED_INSTALL.search(line)
        checked = PRESENCE_PREFIX.match(line)
        probe = CAPABILITY_PROBES.get(installed.group(1)) if installed else None
        if probe is not None and before.endswith(probe):
            continue
        if installed is None or checked is None:
            offenders.append(f"{site}: no presence check in front of the locked install")
        elif checked.group(1).split() != installed.group(1).split():
            offenders.append(
                f"{site}: checks [{checked.group(1)}] but installs [{installed.group(1)}]"
            )
    assert not offenders, "\n".join(offenders)
