"""Tool-cache probes require the completion marker the setup-* actions require.

actions/tool-cache (behind actions/setup-python, setup-node, astral-sh/setup-uv and
mozilla-actions/sccache-action) accepts a cached `<tool>/<version>/<arch>` directory
only when the sibling marker `<tool>/<version>/<arch>.complete` exists. An
interrupted or hand-restored cache can keep a working executable without the
marker; the action then ignores that cache and downloads, which fails on a runner
that cannot. Each probe therefore selects the directory the action would select.

The behavioral test runs each manifest entry's REAL verify command, exactly as
scripts/runner_toolset_verify.py does (`bash -o pipefail -c`), against a cache laid
out under tmp_path in place of /opt/hostedtoolcache. The planted executable is a
wrapper that execs the real tool on this machine, so the command runs for real.

Regression lines:
  - if any tool-cache probe passes on a cache with the executable but no
    `.complete` marker then broken
  - if any tool-cache probe fails on a cache with the executable AND the marker
    then broken
  - if a tool-cache entry's verify never names the `.complete` marker then broken
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import runner_toolset_scan as scan

CACHE_ROOT = "/opt/hostedtoolcache"


def _layout(entry: dict) -> tuple[str, str, str | None]:
    """(arch dir under the cache, executable path inside it, real tool to exec)."""
    version = entry["version"]
    match entry["name"]:
        case "toolcache-python" | "toolcache-python-pytest":
            return "Python/3.11.99/x64", "bin/python3", sys.executable
        case "toolcache-node":
            return f"node/{version}/x64", "bin/node", shutil.which("node")
        case "toolcache-uv":
            return f"uv/{version}/x86_64", "uv", shutil.which("uv")
        case "toolcache-sccache":
            return f"sccache/{version}/x64", "sccache", shutil.which("sccache")
        case other:
            raise AssertionError(f"no cache layout for tool-cache entry {other!r}: add one")


def _tool_cache_entries() -> list[dict]:
    entries = scan.load_manifest()["entries"]
    found = [e for e in entries if CACHE_ROOT in e["verify"]]
    assert len(found) >= 5, f"expected every tool-cache probe, found {[e['name'] for e in found]}"
    return found


ENTRIES = _tool_cache_entries()


def _run_verify(entry: dict, cache: Path) -> subprocess.CompletedProcess[str]:
    command = entry["verify"].replace(CACHE_ROOT, str(cache))
    assert CACHE_ROOT not in command
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", command],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def _plant(entry: dict, cache: Path, *, marker: bool) -> None:
    arch_dir, exe, real = _layout(entry)
    if real is None:
        pytest.skip(f"UNAVAILABLE: no real {Path(exe).name} here to plant in the cache")
    target = cache / arch_dir / exe
    target.parent.mkdir(parents=True)
    target.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n')
    target.chmod(0o755)
    if marker:
        (cache / f"{arch_dir}.complete").write_text("")


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_a_tool_cache_without_its_completion_marker_fails_the_probe(
    entry: dict, tmp_path: Path
) -> None:
    _plant(entry, tmp_path, marker=False)
    result = _run_verify(entry, tmp_path)
    assert result.returncode != 0, f"passed without the marker: {result.stdout}{result.stderr}"
    assert ".complete" in result.stderr + result.stdout, result.stderr


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_a_tool_cache_with_its_completion_marker_passes_the_probe(
    entry: dict, tmp_path: Path
) -> None:
    _plant(entry, tmp_path, marker=True)
    result = _run_verify(entry, tmp_path)
    assert result.returncode == 0, f"failed with the marker: {result.stdout}{result.stderr}"


def test_every_tool_cache_entry_names_its_completion_marker() -> None:
    for entry in scan.load_manifest()["entries"]:
        if entry["kind"] == "toolcache":
            assert ".complete" in entry["verify"], f"{entry['name']}: verify skips the marker"
