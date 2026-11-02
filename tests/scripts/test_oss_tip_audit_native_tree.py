"""oss_tip_audit: the native and Python backends agree on the real tracked tree.

The expensive half of the parity gate (both backends over every published blob, about
15 s with the Python backend dominating), split from (private path)
so a PR head runs it only when it can change: the PR-head selector reaches this module
through its imports (scripts/oss_tip_audit.py, oss_tip_native.py, oss_tip_rules.py,
oss_tip_logins.py) and through the kernel tree's coupling in
scripts/ci_test_selection_rules.py. The cheap seeded-tree parity and the UNKNOWN cases
stay in the other module. The queue and main still run the full suite, this included.

- if the native and Python backends differ on the real tracked tree then broken
- if the native path was not actually taken, or most of the tree went back to Python,
  then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scripts.oss_tip_audit_support import REPO_ROOT, assert_same, build_kernel, run_backend


@pytest.fixture(scope="module")
def kernel() -> Path:
    return build_kernel()


def test_both_backends_agree_on_the_tracked_tree(kernel: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = run_backend(REPO_ROOT, "python", monkeypatch)
    native = run_backend(REPO_ROOT, "native", monkeypatch, kernel)
    assert_same(native, python)
    # The kernel, not the fallback, must have read the tree: most text blobs native.
    assert native.python_fallback * 2 < native.scanned, native
    # A clean tree compares two empty lists; the exempt list is the non-empty half.
    assert python.exempt, "no exempt match on the tree, so the comparison is vacuous"
