"""scripts/ci_frontend_build_reuse.sh: a build restored by frontend tree names THIS commit.

Regression lines:
  - [if] a reused build's _app/version.json still names the old sha [then] fail, [else stop].
  - [if] a dirty or unstamped build is reused without an error [then] fail, [else stop].

-Claude
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("DEVOPS-27")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_frontend_build_reuse.sh"
OLD, NEW = "a" * 40, "b" * 40


def _build(tmp_path: Path, version: str, stamp: bool = True) -> Path:
    build = tmp_path / "build"
    (build / "_app/immutable").mkdir(parents=True)
    (build / "_app/version.json").write_text(f'{{"version":"{version}"}}')
    (build / "_app/immutable/start.js").write_text(f'const v="{version}";')
    (build / "index.html").write_text("<html></html>")
    if stamp:
        (build / ".ci-production-frontend-sha").write_text(f"{OLD}\n")
    return build


def _run(build: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), NEW, str(build)], capture_output=True, text=True)


def test_a_reused_build_names_the_new_sha_everywhere(tmp_path: Path) -> None:
    build = _build(tmp_path, OLD)
    assert _run(build).returncode == 0
    assert NEW in (build / "_app/version.json").read_text()
    assert OLD not in (build / "_app/immutable/start.js").read_text()
    assert (build / ".ci-production-frontend-sha").read_text().strip() == NEW


def test_a_dirty_or_unstamped_build_is_refused(tmp_path: Path) -> None:
    assert _run(_build(tmp_path / "d", f"{OLD}-dirty")).returncode == 1
    assert _run(_build(tmp_path / "u", OLD, stamp=False)).returncode == 1
