"""Shared by the oss_tip_audit test modules: one tracked-tree audit, the kernel, a backend run.

`tracked_tree_audit` runs the full published-tree audit ONCE per pytest process and hands
the same result to every module that asserts on it (test_oss_tip_audit.py and
test_oss_tip_audit_upstream_licenses.py each ran their own before). It is a cached helper,
not a conftest fixture, on purpose: a conftest that imports scripts.oss_tip_audit would make
the PR-head selector pick every test under tests/scripts/ whenever the audit changed
("conftest reach", scripts/ci_test_selection_rules.py).

The kernel helpers are split out so the expensive tracked-tree parity test can live in a module of its own
(tests/scripts/test_oss_tip_audit_native_tree.py), which the PR-head selector reaches
only through the kernel and the audit modules, while the cheap seeded-tree parity and
the UNKNOWN cases stay in (private path).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import ci_oss_audit_kernel, oss_tip_native
from scripts.oss_tip_audit import AuditResult, audit_index, audit_tracked_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRE_ENV = "ODJ_REQUIRE_OSS_AUDIT_NATIVE"


@functools.cache
def tracked_tree_audit() -> AuditResult:
    """`audit_tracked_tree(REPO_ROOT)`, i.e. `audit_index(REPO_ROOT)`, under the process's backend."""
    return audit_tracked_tree(REPO_ROOT)


@functools.cache
def _build() -> tuple[int, str, str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        return -1, "", "no cargo on PATH"
    manifest = oss_tip_native.CRATE_DIR / "Cargo.toml"
    build = subprocess.run(
        [
            cargo,
            "build",
            "--release",
            "--locked",
            "--message-format=json-render-diagnostics",
            "--manifest-path",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return build.returncode, build.stdout, build.stderr


def build_kernel() -> Path:
    """Build the kernel once per process (cargo does nothing when it is fresh) and return it.

    No cargo is a SKIP named UNAVAILABLE, or a failure when REQUIRE_ENV=1 (CI sets it, so a
    shard that lost its toolchain goes red instead of quietly skipping the parity gate).
    """
    code, report, stderr = _build()
    if code == -1:
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(f"{stderr}, and {REQUIRE_ENV}=1 requires the native kernel")
        pytest.skip(f"UNAVAILABLE: {stderr}, so the native kernel cannot be built here")
    assert code == 0, stderr
    binary = ci_oss_audit_kernel.executable_from_cargo(report)
    assert binary is not None, "cargo build succeeded but reported no kernel binary"
    return binary


def run_backend(root: Path, backend: str, monkeypatch: pytest.MonkeyPatch, binary: Path | None = None) -> AuditResult:
    monkeypatch.setenv(oss_tip_native.BACKEND_ENV, backend)
    if binary is not None:
        monkeypatch.setenv(oss_tip_native.BINARY_ENV, str(binary))
    return audit_index(root)


def assert_same(native: AuditResult, python: AuditResult) -> None:
    assert python.backend == "python"
    assert native.backend == "native", "the native path was not taken"
    assert native.findings == python.findings
    assert native.exempt == python.exempt
    assert native.summary() == python.summary()
