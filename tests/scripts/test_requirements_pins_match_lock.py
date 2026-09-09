"""requirements.txt and uv.lock must agree on the JIT toolchain.

Two provisioning paths exist in CI: ci.yml's shard venv comes from
``uv pip install -r requirements.txt`` (free resolution within the pins here),
the e2e job's venv from ``uv sync`` (the lock). Any package pinned here that
the lock also resolves must carry the lock's exact version, or two venvs on one
runner host run different native code.

Requirements:
- [if] requirements.txt pins numba, llvmlite or librosa [then] the pin equals
  the version uv.lock resolves for that package, [else stop]
- [if] uv.lock resolves numba [then] requirements.txt pins it exactly (a
  librosa install with numba unpinned drifts to the newest release), [else stop]
- [if] the lock has no entry for a package under test [then] the test fails
  loudly rather than passing on an absent comparison, [else stop]
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "uv.lock"

# Packages whose native code must not differ between the two venvs.
JIT_TOOLCHAIN = ("numba", "llvmlite")


def _lock_versions() -> dict[str, str]:
    doc = tomllib.loads(LOCK.read_text())
    versions: dict[str, str] = {}
    for pkg in doc["package"]:
        name = pkg["name"]
        if name in versions and versions[name] != pkg["version"]:
            raise AssertionError(
                f"uv.lock resolves {name} to two versions: {versions[name]} and {pkg['version']}"
            )
        versions[name] = pkg["version"]
    return versions


def _requirement_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9.+]*)", line)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


@pytest.mark.parametrize("package", JIT_TOOLCHAIN)
def test_jit_toolchain_is_pinned_to_the_lock(package: str) -> None:
    """[if] uv.lock resolves the package [then] requirements.txt pins that version, [else stop]."""
    lock = _lock_versions()
    assert package in lock, (
        f"uv.lock has no entry for {package}; this test cannot compare an absent version"
    )
    pins = _requirement_pins()
    assert package in pins, (
        f"requirements.txt does not pin {package}; the shard venv would resolve it freely"
    )
    assert pins[package] == lock[package], (
        f"{package}: requirements.txt pins {pins[package]} but uv.lock resolves {lock[package]}"
    )


def test_every_exact_pin_shared_with_the_lock_agrees() -> None:
    """[if] a package is pinned here and in the lock [then] the versions agree, [else stop]."""
    lock = _lock_versions()
    pins = _requirement_pins()
    shared = sorted(set(pins) & set(lock))
    assert shared, (
        "no exact pin in requirements.txt is resolved by uv.lock; the comparison is empty"
    )
    mismatched = {name: (pins[name], lock[name]) for name in shared if pins[name] != lock[name]}
    assert not mismatched, f"requirements.txt and uv.lock disagree: {mismatched}"
