"""No acceptance test may be able to launch an installed app (issue #6267).

`acceptance.sh` and `scripts/app_acceptance.py` launch whatever bundle they
are handed. A test that hands them a real install path only stays harmless
while something on the host happens to refuse: the app is absent, already
running, or the host is silver. On demon-llama on Fri 9 Oct 2026 none of
those held, and a test started the installed app and left it running.

This guard reads test source only. It runs nothing and launches nothing.

[if] a test that can reach the acceptance entry points names a path under /Applications/ [then] this guard fails naming it, [else stop]

Regression lines:
  - if a test function that runs acceptance.sh or app_acceptance.main names a path under /Applications/ then broken
  - if the scan finds none of the acceptance callers that exist today then broken (a scan that sees nothing proves nothing)
  - if the scan misses a planted offender, or flags a planted safe caller, then broken
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPS-54")

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
# A test function that mentions any of these can reach a launch.
# Code-shaped on purpose: a docstring that names acceptance.sh, or a test that only reads the
# script text, cannot launch anything.
ENTRY_MARKERS = ("str(ACCEPTANCE_SH)", "acc.main(", "app_acceptance.main(", "run_acceptance(")
INSTALL_ROOT = "/Applications/"
# Callers known on main when this guard was written. The scan must see at least these.
KNOWN_CALLERS = {
    "test_acceptance_sh_refuses_before_running",
    "test_python_probe_resolves_bash_before_path",
    "test_fixture_staging_failure_writes_a_refused_result",
}


def acceptance_callers(source: str) -> dict[str, str]:
    """Top-level and nested function name -> source, for functions that can reach an acceptance entry point."""
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = ast.get_source_segment(source, node) or ""
        if any(marker in body for marker in ENTRY_MARKERS):
            found[node.name] = body
    return found


def offenders(source: str) -> list[str]:
    """Names of acceptance callers that name a path under the install root."""
    return sorted(name for name, body in acceptance_callers(source).items() if INSTALL_ROOT in body)


def _test_sources() -> dict[Path, str]:
    this_file = Path(__file__).resolve()
    sources: dict[Path, str] = {}
    for path in sorted(TESTS.rglob("*.py")):
        if path.resolve() == this_file:
            continue  # this file names the markers and the root in order to look for them
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in ENTRY_MARKERS):
            sources[path] = text
    return sources


def test_the_scan_flags_a_planted_offender_and_passes_a_planted_safe_caller() -> None:
    """if the scan misses a planted offender, or flags a planted safe caller, then broken"""
    planted = (
        "def test_offender():\n"
        "    subprocess.run(['/bin/bash', str(ACCEPTANCE_SH), '--machine', 'here',\n"
        "                    '--app', '/Applications/Open DJ.app'])\n"
        "\n"
        "def test_safe(tmp_path):\n"
        "    acc.main(['--app', str(tmp_path / 'Absent.app')])\n"
        "\n"
        "def test_not_a_caller():\n"
        "    \"\"\"acceptance.sh must quote the path\"\"\"\n"
        "    assert '/Applications/Open DJ.app' in ACCEPTANCE_SH.read_text()\n"
    )
    assert sorted(acceptance_callers(planted)) == ["test_offender", "test_safe"]
    assert offenders(planted) == ["test_offender"]


def test_no_acceptance_test_names_an_installed_app() -> None:
    """if a test function that runs acceptance.sh or app_acceptance.main names a path under /Applications/ then broken"""
    sources = _test_sources()
    seen = {name for text in sources.values() for name in acceptance_callers(text)}
    missing = KNOWN_CALLERS - seen
    assert not missing, f"the scan did not see known acceptance callers {sorted(missing)}: it cannot vouch for anything"
    found = {str(path.relative_to(REPO)): offenders(text) for path, text in sources.items() if offenders(text)}
    assert not found, (
        f"these tests hand a real install path to an acceptance entry point: {found}. "
        "Use a path under tmp_path so the refusal does not depend on what is installed or running."
    )
