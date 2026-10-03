"""apps.shared.disposable_dirs: fixture builders refuse protected roots before any rmtree.

Regression one-liners:
  - if a builder target equal to, inside, or containing a protected root is accepted then broken
  - if an extra protected root (a fixture's own source library) is not honored then broken
  - if an ordinary scratch directory is refused then broken
  - if the guard derives its roots from MDT_DATA_DIR or $HOME then broken
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.disposable_dirs import refuse_protected_target
from apps.shared.paths import PROJECT_ROOT


@pytest.mark.parametrize(
    "target",
    [
        PROJECT_ROOT / "data",
        PROJECT_ROOT / "data" / "fixture",
        Path.home() / "Library" / "Pioneer" / "rekordbox",
        Path.home() / "Library",
        Path.home() / "Library" / "Application Support" / "com.opendj.desktop",
    ],
)
def test_refuses_a_target_overlapping_a_protected_root(target: Path) -> None:
    with pytest.raises(SystemExit, match="refusing to use"):
        refuse_protected_target(target)


def test_refuses_a_target_inside_an_extra_protected_source(tmp_path: Path) -> None:
    source = tmp_path / "source-library"
    with pytest.raises(SystemExit, match="refusing to use"):
        refuse_protected_target(source / "copy", source)


def test_accepts_an_ordinary_scratch_directory(tmp_path: Path) -> None:
    refuse_protected_target(tmp_path / "disposable")


def test_a_builder_running_under_its_own_mdt_data_dir_is_not_refused(tmp_path: Path) -> None:
    """The overlay builder runs with MDT_DATA_DIR set to its own target (PR #3832 P1)."""
    target = tmp_path / "fixture-data"
    probe = (
        "import sys; from pathlib import Path; "
        "from apps.shared.disposable_dirs import refuse_protected_target; "
        "refuse_protected_target(Path(sys.argv[1])); print('allowed')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(target)],
        # The overlay e2e also points HOME at a sandbox inside the target.
        env={**os.environ, "MDT_DATA_DIR": str(target), "HOME": str(target / "sandbox-home")},
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "allowed"
