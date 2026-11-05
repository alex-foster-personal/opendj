"""AST gate: no raw ``os.kill(<x>, 0)`` liveness probes outside the platform seam.

On Windows signal 0 is ``CTRL_C_EVENT`` and interrupts the whole console group.
Every liveness probe under ``apps/`` and ``scripts/`` must go through
``apps/shared/process_liveness.py`` (POSIX arm) or the census allowlist.

[if] a tracked apps/ or scripts/ module outside the allowlist spells
os.kill(<x>, 0) [then] this scan names it, [else stop].

Regression lines:
  - if git ls-files returns nothing then the scan is unmeasured, not clean
  - if an allowlisted file stops spelling os.kill(pid, 0) then update the allowlist
  - if os.killpg(pgid, 0) or os.kill(pid, 15) or a comment matches then broken
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

ALLOWED = frozenset(
    {
        "apps/shared/process_liveness.py",
        "scripts/pytest_tree_read_census.py",
    }
)


def _is_raw_kill_zero(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "kill"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == 0
    )


def source_has_raw_kill_zero(text: str) -> bool:
    tree = ast.parse(text)
    return any(_is_raw_kill_zero(node) for node in ast.walk(tree))


def _tracked_py_files(*roots: str) -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", *roots],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names = [name for name in listing.split("\0") if name.endswith(".py")]
    assert names, f"git ls-files returned no .py files for {roots}; the scan root is wrong"
    return names


def _files_with_raw_kill_zero(*roots: str) -> set[str]:
    found: set[str] = set()
    for name in _tracked_py_files(*roots):
        text = (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text, filename=name)
        except SyntaxError as exc:
            raise AssertionError(f"{name} did not parse: {exc}") from exc
        if any(_is_raw_kill_zero(node) for node in ast.walk(tree)):
            found.add(name)
    return found


def test_no_raw_kill_zero_outside_allowlist() -> None:
    found = _files_with_raw_kill_zero("apps", "scripts")
    offenders = found - ALLOWED
    assert not offenders, f"raw os.kill(<x>, 0) outside the platform seam: {sorted(offenders)}"


def test_scan_sees_the_allowlisted_files() -> None:
    found = _files_with_raw_kill_zero("apps", "scripts")
    missing = ALLOWED - found
    assert not missing, f"allowlisted files no longer contain os.kill(<x>, 0); update ALLOWED: {sorted(missing)}"


def test_tracked_file_probe_both_directions() -> None:
    present = {
        name
        for name in _tracked_py_files("apps")
        if "SCHEMA_VERSION" in (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
    }
    absent = {
        name
        for name in _tracked_py_files("apps")
        if "af_probe_no_such_token_anywhere" in (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
    }
    assert "apps/shared/state/schema.py" in present
    assert absent == set()


def test_matcher_finds_os_kill_zero() -> None:
    assert source_has_raw_kill_zero("import os\nos.kill(pid, 0)\n")


def test_matcher_ignores_killpg_real_signals_and_comments() -> None:
    assert not source_has_raw_kill_zero("import os\nos.killpg(pgid, 0)\n")
    assert not source_has_raw_kill_zero("import os\nos.kill(pid, 15)\n")
    assert not source_has_raw_kill_zero("import os, signal\nos.kill(pid, signal.SIGTERM)\n")
    assert not source_has_raw_kill_zero("# os.kill(pid, 0)\n")
