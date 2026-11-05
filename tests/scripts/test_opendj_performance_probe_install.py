"""Install-copy smoke and launcher title for the self-contained performance probe.

The launchd job copies only ``scripts/diagnostics/*.py``. A venv editable
install of ``apps`` would hide a shim crash, so the smoke test runs
``python -S`` with ``PYTHONNOUSERSITE=1`` and ``HOME`` under ``tmp_path``.

- [if] the copied diagnostics package is isolated from apps [then] --help exits 0 without importing apps, launchers set argv[0] via exec -a, and the machine sampler pins SYSCTL, [else stop].
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.diagnostics.probe_machine_metrics import SYSCTL, try_sysctl_n

pytestmark = pytest.mark.requirement("DEVLOOP-11")

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIAGNOSTICS = ROOT / "scripts" / "diagnostics"
PLIST_PATH = SCRIPTS_DIAGNOSTICS / "com.opendj.performance-probe.plist"
JUSTFILE_PATH = ROOT / "justfile"
PROBE_TITLE = "opendj-performance-probe --name opendj-performance-probe"


def _copy_install_package(tmp_path: Path) -> Path:
    dest = tmp_path / "bin" / "opendj_diagnostics"
    dest.mkdir(parents=True)
    for path in SCRIPTS_DIAGNOSTICS.glob("*.py"):
        shutil.copy2(path, dest / path.name)
    return tmp_path / "bin"


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "PYTHONNOUSERSITE": "1"}


def _imported_top_levels(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".", 1)[0])
    return names


def _just_recipe_body(justfile: str, name: str) -> str:
    lines = justfile.splitlines()
    start = None
    for index, line in enumerate(lines):
        head = line.split(":", 1)[0]
        if head == name or head.startswith(f"{name} "):
            start = index
            break
    assert start is not None, f"just recipe {name} is missing"
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def _plist_program_arguments() -> list[str]:
    tree = ET.parse(PLIST_PATH)
    children = list(tree.getroot().find("dict"))
    for index, child in enumerate(children):
        if child.tag == "key" and (child.text or "") == "ProgramArguments":
            array = children[index + 1]
            return [(item.text or "") for item in array.findall("string")]
    raise AssertionError("ProgramArguments missing from performance-probe plist")


def test_install_copy_help_exits_zero(tmp_path: Path) -> None:
    bin_dir = _copy_install_package(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "opendj_diagnostics.opendj_performance_probe", "--help"],
        cwd=bin_dir,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="macOS --once samples; Linux reports proc_pid_rusage unavailable",
)
def test_install_copy_once_reports_unavailable(tmp_path: Path) -> None:
    bin_dir = _copy_install_package(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "opendj_diagnostics.opendj_performance_probe", "--once"],
        cwd=bin_dir,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload.get("available") is False


def test_copied_modules_do_not_import_apps(tmp_path: Path) -> None:
    bin_dir = _copy_install_package(tmp_path)
    copied = list((bin_dir / "opendj_diagnostics").glob("*.py"))
    assert copied, "install copy is empty"
    offenders = [path.name for path in copied if "apps" in _imported_top_levels(path)]
    assert not offenders, f"copied modules import apps: {offenders}"
    repo_offenders = [path.name for path in SCRIPTS_DIAGNOSTICS.glob("*.py") if "apps" in _imported_top_levels(path)]
    assert not repo_offenders, f"scripts/diagnostics imports apps: {repo_offenders}"


def test_launchers_set_argv0_with_exec_a() -> None:
    args = _plist_program_arguments()
    assert args[:2] == ["/bin/sh", "-c"]
    script = args[2]
    assert "exec -a" in script
    exec_at = script.index("exec -a")
    python_at = script.index("/usr/bin/python3")
    assert exec_at < python_at
    assert PROBE_TITLE in script[exec_at:python_at]

    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    assert f'probe-process-title := "{PROBE_TITLE}"' in justfile
    for name in ("probe-once", "probe-summary", "probe-trend"):
        body = _just_recipe_body(justfile, name)
        assert 'exec -a "{{probe-process-title}}"' in body, name


def _exec_a_os_argv0(shell: str) -> subprocess.CompletedProcess[str]:
    # CPython -c leaves sys.argv[0] as "-c"; exec -a changes OS argv[0],
    # which is what ps -o args= reads. /proc/self/cmdline is that OS vector.
    pycode = "import pathlib;print(pathlib.Path('/proc/self/cmdline').read_bytes().split(bytes([0]))[0].decode())"
    return subprocess.run(
        [
            shell,
            "-c",
            f'exec -a \'{PROBE_TITLE}\' "$1" -c "$2"',
            "sh",
            sys.executable,
            pycode,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/cmdline").exists(),
    reason="exec -a OS argv[0] is observed via /proc/self/cmdline",
)
def test_exec_a_sets_sys_argv0() -> None:
    # The plist uses /bin/sh (bash 3.2 on macOS). Linux /bin/sh may be a
    # dash/ash whose exec has no -a (`exec: -a: not found`); just recipes use
    # bash, which is the fallback that still proves the launcher mechanism.
    completed = _exec_a_os_argv0("/bin/sh")
    if completed.returncode != 0:
        bash = shutil.which("bash")
        if bash is None:
            pytest.fail(completed.stderr)
        completed = _exec_a_os_argv0(bash)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == PROBE_TITLE


def test_scripts_machine_sampler_pins_sysctl() -> None:
    assert SYSCTL == "/usr/sbin/sysctl"
    source = inspect.getsource(try_sysctl_n)
    assert "SYSCTL" in source
    assert '"sysctl"' not in source
    assert "'sysctl'" not in source
