"""Cargo build scripts' system libraries are declared in the runner toolset.

A crate whose build script loads a system library reaches the runner through
Cargo, which the workflow scan in test_runner_toolset_complete.py cannot see.
Found live: signalsmith-stretch (#4459) builds through bindgen, whose clang-sys
needs libclang, and reqs-check failed on agentbox with "Unable to find libclang"
(PR #4361, Wed 30 Sep 2026).

Regression lines:
  - if a tracked Cargo.lock carries clang-sys (bindgen's libclang loader) and no
    libclang apt entry is declared then broken
  - if a crate whose name merely starts with clang-sys counts as clang-sys then broken
"""

from __future__ import annotations

import copy
import fnmatch
import subprocess
import tomllib

import pytest

from scripts import runner_toolset_scan as scan

# Crates whose build script needs a system library, a dependency no workflow line
# names: it reaches the runner through a Cargo build script, which the shell scan
# cannot see. A tracked Cargo.lock carrying one requires a manifest apt entry
# matching the glob. Value: (apt entry name glob, why).
CARGO_SYSTEM_LIBRARIES = {
    "clang-sys": ("libclang1*", "bindgen loads libclang through clang-sys at build time"),
}

KNOWN_MISSES_ENTRIES = {
    "libglib2.0-dev", "libgtk-3-dev", "libwebkit2gtk-4.1-dev", "libsoup-3.0-dev",
    "libjavascriptcoregtk-4.1-dev", "libayatana-appindicator3-dev", "librsvg2-dev", "libxdo-dev",
    "libdbus-1-dev", "toolcache-python-pytest",
}  # fmt: skip


def tracked_cargo_lockfiles() -> dict[str, str]:
    """Every tracked Cargo.lock, by repo-relative path, from the index (not a disk walk)."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*Cargo.lock"],
        cwd=scan.REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        rel: (scan.REPO_ROOT / rel).read_text(encoding="utf-8") for rel in listed.split("\0") if rel
    }


def cargo_system_library_gaps(manifest: dict, lockfiles: dict[str, str]) -> list[str]:
    """One line per lockfile crate in CARGO_SYSTEM_LIBRARIES with no matching apt entry."""
    apt = [e["name"] for e in manifest["entries"] if e["kind"] == "apt"]
    gaps = []
    for rel, text in sorted(lockfiles.items()):
        crates = {pkg["name"] for pkg in tomllib.loads(text).get("package", [])}
        for crate in sorted(crates & set(CARGO_SYSTEM_LIBRARIES)):
            glob, why = CARGO_SYSTEM_LIBRARIES[crate]
            if not any(fnmatch.fnmatchcase(name, glob) for name in apt):
                gaps.append(f"{rel} carries {crate} ({why}); declare an apt entry matching {glob}")
    return gaps


def _scan_text(shell: str = "", python: str = "") -> scan.Usage:
    ctx = scan._Ctx(usage=scan.Usage())
    if shell:
        scan.scan_shell(shell, "probe.sh", 0, ctx)
    if python:
        scan.scan_python_source(python, "probe.py", 0, ctx)
    return ctx.usage


# ----- the contract -----------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    return scan.load_manifest()


def test_every_cargo_system_library_is_declared(manifest: dict) -> None:
    lockfiles = tracked_cargo_lockfiles()
    assert lockfiles, "git ls-files found no Cargo.lock: the scan is broken, not clean"
    gaps = cargo_system_library_gaps(manifest, lockfiles)
    assert not gaps, "Cargo build scripts need system libraries the manifest lacks:\n  " + (
        "\n  ".join(gaps)
    )


def test_deleting_libclang_from_the_manifest_goes_red(manifest: dict) -> None:
    """Mutation control, from the defect: signalsmith-stretch's bindgen failed with
    'Unable to find libclang' on agentbox (PR #4361, Wed 30 Sep 2026)."""
    mutated = copy.deepcopy(manifest)
    mutated["entries"] = [e for e in mutated["entries"] if not e["name"].startswith("libclang")]
    gaps = cargo_system_library_gaps(mutated, tracked_cargo_lockfiles())
    assert any(g.startswith("apps/audio-engine/Cargo.lock carries clang-sys") for g in gaps), gaps


LOCK_WITH = '[[package]]\nname = "{}"\nversion = "1.0.0"\n'


@pytest.mark.parametrize(
    ("crate", "entry", "gap"),
    [
        ("clang-sys", None, True),
        ("clang-sys", {"name": "libclang1", "kind": "apt"}, False),
        ("clang-sys", {"name": "libclang1-19", "kind": "apt"}, False),
        ("clang-sys", {"name": "libclang1-18", "kind": "binary"}, True),
        ("clang-sys", {"name": "libclang-dev", "kind": "apt"}, True),
        ("clang-sys-extra", None, False),
        ("serde", None, False),
    ],
)
def test_a_cargo_system_library_needs_its_apt_entry(
    crate: str, entry: dict | None, gap: bool
) -> None:
    """Positive: clang-sys with no libclang1* apt entry is a gap. Negative controls:
    the version-neutral libclang1 or another LLVM's libclang1-NN satisfies it, a
    crate whose name only starts with clang-sys or a crate with no system library
    requires nothing, and a same-named entry of another kind or a differently
    named package does not count."""
    manifest = {"entries": [entry] if entry else []}
    gaps = cargo_system_library_gaps(manifest, {"probe/Cargo.lock": LOCK_WITH.format(crate)})
    assert bool(gaps) is gap, gaps
