"""The verifier's apt ownership probe, run for real over this host's dpkg database.

dpkg-query vouches for a package, not for the executable a job runs, so the
probe records the package owning the file each apt executable resolves to on
the job PATH (symlinks and alternatives followed, merged-/usr paths looked up
under /bin too). Every case runs the verifier's own probe script against real
installed files; a host without dpkg reports UNAVAILABLE.

Regression lines:
  - if an apt entry passes for an executable its package does not own then broken
  - if an executable no package owns, shadowing the system one, passes then broken
  - if a symlink or alternatives link is judged by the link, not the file it
    resolves to, or a merged-/usr file dpkg records under /bin reads as unowned,
    then broken
  - if a job PATH without dpkg-query yields a verdict instead of UNKNOWN then broken
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.scripts.test_runner_toolset_verify_probe import (
    PROBE_NEEDS,
    _dirs_of,
    _probe_locally,
    _runner_with_path,
)


def _apt_entry(package: str, exe: str) -> dict:
    return {
        "name": package,
        "kind": "apt",
        "version": "0",
        "match": "min",
        "verify": f"dpkg-query -W -f='${{db:Status-Abbrev}}${{Version}}' {package} | grep ^ii",
        "provides": [exe],
    }


def _dpkg_owner(path: str) -> str | None:
    if not shutil.which("dpkg-query"):
        pytest.skip("UNAVAILABLE: no dpkg on this host, so there is no package ownership")
    found = subprocess.run(["dpkg-query", "-S", path], capture_output=True, text=True, check=False)
    return found.stdout.split(":")[0] if found.returncode == 0 else None


@pytest.mark.parametrize(
    ("package", "exe", "status"),
    [
        ("tar", "tar", "OK"),
        ("dash", "sh", "OK"),
        ("iproute2", "ss", "OK"),
        ("tar", "gzip", "MISMATCH"),
    ],
)
def test_the_probe_checks_each_apt_executable_against_the_real_package_database(
    tmp_path: Path, package: str, exe: str, status: str
) -> None:
    """`sh` follows a symlink; `ss` resolves to /usr/bin/ss, which dpkg records
    only as /bin/ss (merged /usr); `tar` declaring `gzip` is the mawk-declares-awk
    defect: a real file, another package's."""
    if not shutil.which(exe):
        pytest.skip(f"UNAVAILABLE: no {exe} installed here")
    if exe == "sh" and Path("/bin/sh").resolve().name != "dash":
        pytest.skip("UNAVAILABLE: /bin/sh is not dash here, so there is no dash-owned sh")
    _dpkg_owner("/bin/sh")
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, "dpkg-query", "grep", exe))
    [result], _ = _probe_locally([_apt_entry(package, exe)], runner)
    assert result.status == status, result


def test_the_probe_rejects_an_apt_executable_no_package_owns(tmp_path: Path) -> None:
    """This test's own interpreter, when no package installed it (a uv or tool-cache
    Python), shadows the system python3 exactly as a stray binary would."""
    interpreter = Path(sys.executable).resolve()
    if _dpkg_owner(str(interpreter)) is not None:
        pytest.skip(f"UNAVAILABLE: {interpreter} is dpkg-owned here, so it cannot shadow")
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    (shadow_dir / "python3").symlink_to(interpreter)
    runner = _runner_with_path(tmp_path, [str(shadow_dir), *_dirs_of(*PROBE_NEEDS, "dpkg-query")])
    [result], _ = _probe_locally([_apt_entry("python3", "python3")], runner)
    assert result.status == "MISMATCH" and "no package owns it" in result.got, result


def test_an_ownership_lookup_with_no_dpkg_on_the_job_path_is_unknown(tmp_path: Path) -> None:
    """A job PATH without dpkg-query cannot measure ownership: UNKNOWN, not MISMATCH."""
    _dpkg_owner("/usr/bin/tar")
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in (*PROBE_NEEDS, "readlink", "tar"):
        real = shutil.which(tool)
        assert real, f"probe prerequisite {tool} absent"
        (tools / tool).symlink_to(real)
    assert not (tools / "dpkg-query").exists()
    runner = _runner_with_path(tmp_path, [str(tools)])
    entry = {**_apt_entry("tar", "tar"), "verify": "tar --version"}
    [result], _ = _probe_locally([entry], runner)
    assert result.status == "UNKNOWN" and "not measured" in result.got, result


def test_an_alternatives_symlink_is_followed_to_the_package_that_owns_its_target(
    tmp_path: Path,
) -> None:
    """/usr/bin/awk is an update-alternatives link no package owns; the file a job
    runs is its target (gawk or mawk), so that target's package must be the one."""
    awk = shutil.which("awk")
    if not awk or not Path(awk).is_symlink():
        pytest.skip("UNAVAILABLE: no alternatives-managed awk here")
    assert awk is not None  # the gate's isolated mypy cannot see pytest.skip is NoReturn
    target = str(Path(awk).resolve())
    owner = _dpkg_owner(target) or _dpkg_owner(target.removeprefix("/usr"))
    if _dpkg_owner(awk) is not None or owner is None:
        pytest.skip(f"UNAVAILABLE: {awk} is package-owned itself, or {target} is unowned")
    assert owner is not None  # as above: narrowing, not a second check
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, "dpkg-query", "awk"))
    [result], _ = _probe_locally([_apt_entry(owner, "awk")], runner)
    assert result.status == "OK", result
