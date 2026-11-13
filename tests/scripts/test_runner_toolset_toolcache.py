"""Tool-cache probes require the completion marker the setup-* actions require.

actions/tool-cache (behind actions/setup-python, setup-node, astral-sh/setup-uv and
mozilla-actions/sccache-action) accepts a cached `<tool>/<version>/<arch>` directory
only when the sibling marker `<tool>/<version>/<arch>.complete` exists. An
interrupted or hand-restored cache can keep a working executable without the
marker; the action then ignores that cache and downloads, which fails on a runner
that cannot. Each probe therefore selects the directory the action would select.

Everything here runs against the host's REAL action-populated cache, never a
fabricated one: the positive case runs each manifest entry's verify command
unchanged against it, exactly as the host verifier (fleet-af runner_toolset/verify.py) does (`bash -o
pipefail -c`). The negative case copies one real version directory, its real
bytes and its real marker, into a temporary cache root, runs the same command
there, then deletes the copied marker and runs it again: the marker is the only
difference. A host with no populated cache for an entry (a dev Mac) reports
UNAVAILABLE.

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
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import runner_toolset_scan as scan

# The directory every manifest probe names; the runner sets RUNNER_TOOL_CACHE to it
# (AGENT_TOOLSDIRECTORY in each runner's .env).
MANIFEST_CACHE = "/opt/hostedtoolcache"

# A copy of a real toolchain is a hardlink farm when the filesystem allows it and
# 1.05 GB of bytes (40,369 files for CPython 3.11.16) when it does not, so the
# byte path is bounded here instead of being left to the tier's per-test timeout.
COPY_BUDGET_S = 60.0


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
        case "toolcache-uvx":
            return "uv/*/x86_64", "uvx"
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


class CopyBudgetExceeded(Exception):
    """The copy fell back to real bytes and would outlive the job's budget.

    Deliberately not an OSError: shutil.copytree collects OSError per entry and
    carries on, which is exactly how a whole-toolchain byte copy outlives a
    timeout instead of stopping.
    """


def _copy_parents(arch_dir: Path) -> list[Path]:
    """Writable places to keep a copy, `arch_dir`'s own filesystem first."""
    return [Path(tempfile.gettempdir()), *arch_dir.resolve().parents]  # never inside the tree


# `os.link` is what makes the negative case cheap: a real version dir is 1.05 GB
# of bytes but 41,833 hardlinks. It cannot cross filesystems, and pytest's
# tmp_path is not on the cache's filesystem on a self-hosted runner: RUNNER_TEMP
# there is a separate 2 GB tmpfs (dev 69 on nucbox runner-3) while
# /opt/hostedtoolcache is on /dev/sdd (dev 2096), so every one of the 41,833
# entries raised EXDEV and the byte fallback wrote 1,048,130,326 bytes into that
# 2 GB tmpfs until the 120 s per-test timeout killed the fast leg (job
# 118568630420, 2026-10-20). A root on the source's own device keeps the hardlink
# path the one that runs. A hosted runner, where tmp_path already shares the
# device, is unchanged; /tmp is the next choice; a host with neither (a read-only
# cache filesystem) byte-copies into tmp_path under COPY_BUDGET_S.
def _copy_root(arch_dir: Path, tmp_path: Path) -> Path:
    """A fresh, disposable cache root to copy `arch_dir` into, on `arch_dir`'s device."""
    dev = os.stat(arch_dir).st_dev
    for parent in (tmp_path, *_copy_parents(arch_dir)):
        try:
            if os.stat(parent).st_dev != dev or not os.access(parent, os.W_OK | os.X_OK):
                continue
        except OSError:
            continue
        return Path(tempfile.mkdtemp(prefix="toolcache-copy-", dir=parent))
    return Path(tempfile.mkdtemp(prefix="toolcache-copy-", dir=tmp_path))


def _link_else_copy(deadline: float) -> Callable[[str, str], str]:
    """copytree copy_function: hardlink, byte-copy a lone refuser, stop at the budget."""

    def copy_entry(src: str, dst: str) -> str:
        if time.monotonic() > deadline:
            raise CopyBudgetExceeded(f"copy budget {COPY_BUDGET_S:.0f}s exhausted")
        try:
            os.link(src, dst)
        except OSError:
            return shutil.copy2(src, dst)
        return dst

    return copy_entry


def _copy_real_version(arch_dir: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Hardlink (else copy) the real version dir's bytes and its real marker into
    a copy-shaped cache root; return `(cache_root, copied_marker)`.

    A copy that cannot finish inside COPY_BUDGET_S reports UNAVAILABLE with the
    reason it measured rather than sitting in the tier's timeout: the positive
    case above is what proves the real cache, and a copy too large for the budget
    is a fact about the host, not a product regression.
    """
    root = _copy_root(arch_dir, tmp_path)
    target = root / arch_dir.relative_to(_real_cache())
    try:
        shutil.copytree(
            arch_dir, target, symlinks=True,
            copy_function=_link_else_copy(time.monotonic() + COPY_BUDGET_S),
        )  # fmt: skip
        return root, Path(shutil.copy2(f"{arch_dir}.complete", f"{target}.complete"))
    except (OSError, CopyBudgetExceeded) as exc:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"UNAVAILABLE: no copy of {arch_dir} within {COPY_BUDGET_S:.0f}s: {exc}")
        raise  # unreachable: skip raises; keeps the return type total for mypy


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_the_real_marked_tool_cache_passes_the_probe(entry: dict) -> None:
    _populated_arch_dir(entry)
    result = _run_verify(entry, _real_cache())
    assert result.returncode == 0, f"failed on the real cache: {result.stdout}{result.stderr}"


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_a_real_cache_copy_passes_only_with_its_marker(entry: dict, tmp_path: Path) -> None:
    root, marker = _copy_real_version(_populated_arch_dir(entry), tmp_path)
    try:
        control = _run_verify(entry, root)
        assert control.returncode == 0, f"the marked copy failed: {control.stdout}{control.stderr}"
        marker.unlink()
        result = _run_verify(entry, root)
        assert result.returncode != 0, f"passed without the marker: {result.stdout}{result.stderr}"
        assert ".complete" in result.stdout + result.stderr, result.stdout + result.stderr
    finally:
        # Free the copy now: RUNNER_TEMP is a 2G tmpfs on nucbox shared with the
        # whole shard, and pytest keeps every tmp_path until the session ends
        # (ENOSPC on nucbox-wsl-3/6/8/14, Fri 2 Oct 2026).
        shutil.rmtree(root, ignore_errors=True)


def test_every_tool_cache_entry_names_its_completion_marker() -> None:
    for entry in ENTRIES:
        assert ".complete" in entry["verify"], f"{entry['name']}: verify skips the marker"
