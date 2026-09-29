"""Tool-cache probes require the completion marker the setup-* actions require.

actions/tool-cache (behind actions/setup-python, setup-node, astral-sh/setup-uv and
mozilla-actions/sccache-action) accepts a cached `<tool>/<version>/<arch>` directory
only when the sibling marker `<tool>/<version>/<arch>.complete` exists. An
interrupted or hand-restored cache can keep a working executable without the
marker; the action then ignores that cache and downloads, which fails on a runner
that cannot. Each probe therefore selects the directory the action would select.

Everything here runs against the host's REAL action-populated cache, never a
fabricated one: the positive case runs each manifest entry's verify command
unchanged against it, exactly as scripts/runner_toolset_verify.py does (`bash -o
pipefail -c`). The negative case copies one real version directory, its real
bytes and its real marker, into tmp_path, runs the same command there, then
deletes the copied marker and runs it again: the marker is the only difference.
A host with no populated cache for an entry (a dev Mac) reports UNAVAILABLE.

Regression lines:
  - if any tool-cache probe passes on a real cache directory whose `.complete`
    marker is absent then broken
  - if any tool-cache probe fails on the host's real, marked cache then broken
  - if a tool-cache entry's verify never names the `.complete` marker then broken
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import runner_toolset_scan as scan

# The directory every manifest probe names; the runner sets RUNNER_TOOL_CACHE to it
# (AGENT_TOOLSDIRECTORY in each runner's .env).
MANIFEST_CACHE = "/opt/hostedtoolcache"


def _real_cache() -> Path:
    return Path(os.environ.get("RUNNER_TOOL_CACHE") or MANIFEST_CACHE)


def _layout(entry: dict) -> tuple[str, str]:
    """(glob for the entry's `<tool>/<version>/<arch>` dirs, what must exist inside)."""
    version = entry["version"]
    match entry["name"]:
        case "toolcache-python":
            return "Python/3.11.*/x64", "bin/python3"
        case "toolcache-python-pytest":
            return "Python/3.11.*/x64", "lib/python3.11/site-packages/pytest"
        case "toolcache-node":
            return f"node/{version}/x64", "bin/node"
        case "toolcache-uv":
            return "uv/*/x86_64", "uv"
        case "toolcache-sccache":
            return f"sccache/{version}/x64", "sccache"
        case other:
            raise AssertionError(f"no cache layout for tool-cache entry {other!r}: add one")


def _tool_cache_entries() -> list[dict]:
    entries = scan.load_manifest()["entries"]
    found = [e for e in entries if MANIFEST_CACHE in e["verify"]]
    assert len(found) >= 5, f"expected every tool-cache probe, found {[e['name'] for e in found]}"
    return found


ENTRIES = _tool_cache_entries()


def _populated_arch_dir(entry: dict) -> Path:
    """The newest real, action-marked cache dir holding what the entry probes."""
    pattern, inside = _layout(entry)
    cache = _real_cache()
    marked = [
        d for d in cache.glob(pattern)
        if Path(f"{d}.complete").is_file() and (d / inside).exists()
    ]  # fmt: skip
    if not marked:
        pytest.skip(f"UNAVAILABLE: no action-populated {pattern} holding {inside} under {cache}")
    return max(marked, key=lambda d: [int(p) for p in d.parent.name.split(".") if p.isdigit()])


def _run_verify(entry: dict, cache: Path) -> subprocess.CompletedProcess[str]:
    command = entry["verify"].replace(MANIFEST_CACHE, str(cache))
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", command],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


def _copy_real_version(arch_dir: Path, into: Path) -> Path:
    """Hardlink (else copy) the real version dir's bytes and its real marker into
    `into`, same layout; return the copied marker."""
    target = into / arch_dir.relative_to(_real_cache())
    try:
        shutil.copytree(arch_dir, target, symlinks=True, copy_function=os.link)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(arch_dir, target, symlinks=True)
    return Path(shutil.copy2(f"{arch_dir}.complete", f"{target}.complete"))


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_the_real_marked_tool_cache_passes_the_probe(entry: dict) -> None:
    _populated_arch_dir(entry)
    result = _run_verify(entry, _real_cache())
    assert result.returncode == 0, f"failed on the real cache: {result.stdout}{result.stderr}"


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_a_real_cache_copy_passes_only_with_its_marker(entry: dict, tmp_path: Path) -> None:
    marker = _copy_real_version(_populated_arch_dir(entry), tmp_path)

    control = _run_verify(entry, tmp_path)
    assert control.returncode == 0, f"the marked copy failed: {control.stdout}{control.stderr}"
    marker.unlink()
    result = _run_verify(entry, tmp_path)
    assert result.returncode != 0, f"passed without the marker: {result.stdout}{result.stderr}"
    assert ".complete" in result.stdout + result.stderr, result.stdout + result.stderr


def test_every_tool_cache_entry_names_its_completion_marker() -> None:
    for entry in ENTRIES:
        assert ".complete" in entry["verify"], f"{entry['name']}: verify skips the marker"
