"""Every real-library test carries the ``real_library`` marker, and only those.

``just cloudsync-fast`` deselects ``real_library`` and ``just cloudsync-slow``
selects it, so a real-library test without the marker would run in the fast
tier (and skip there) while the slow tier's executed count silently missed
it. The marker is derived in ``conftest.py`` from the fixtures a test
requests; this checks the result through a real ``--collect-only`` run.

Single-line intent:
  - if a real-library test escapes the real_library marker then broken
  - if the marker lands on a test that does not need the library then broken

[if] a real-library test is collected [then] only it carries the real_library marker, [else stop].
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("CAT-04")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
REAL_LIBRARY_MODULE_PREFIX: str = "tests/cloudsync/test_real_library_"
#: Modules in this directory that hold no real-library test by design.
NOT_REAL_LIBRARY_MODULES: tuple[str, ...] = (
    "tests/cloudsync/test_real_library_source.py",
    "tests/cloudsync/test_real_library_tier_marker.py",
)


def _collected(marker_expression: str) -> list[str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/cloudsync",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            marker_expression,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [line for line in completed.stdout.splitlines() if "::" in line]


def _is_real_library_module(nodeid: str) -> bool:
    module = nodeid.split("::", 1)[0]
    return module.startswith(REAL_LIBRARY_MODULE_PREFIX) and (
        module not in NOT_REAL_LIBRARY_MODULES
    )


def test_every_real_library_test_is_marked_and_nothing_else_is() -> None:
    """if a real-library test escapes the real_library marker then broken"""
    marked = _collected("real_library")
    unmarked = _collected("not real_library")

    assert marked, "no test carries real_library, so the slow tier is empty"
    assert unmarked, "the complement is empty, so the selection is not working"
    wrongly_marked = [nodeid for nodeid in marked if not _is_real_library_module(nodeid)]
    escaped = [nodeid for nodeid in unmarked if _is_real_library_module(nodeid)]
    assert wrongly_marked == [], f"marked but not real-library: {wrongly_marked}"
    assert escaped == [], f"real-library tests missing the marker: {escaped}"
