"""Integration test: `mkdocs build --strict` must succeed.

Skipped when mkdocs / mkdocs-material are not installed (local dev,
test-only venvs). CI installs them via `requirements-docs.txt`.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mkdocs_available():
    pytest.importorskip("mkdocs", reason="mkdocs not installed; skipping strict build")
    pytest.importorskip(
        "material", reason="mkdocs-material not installed; skipping strict build"
    )
    if shutil.which("mkdocs") is None:
        pytest.skip("mkdocs executable not on PATH")
    return True


def test_mkdocs_build_strict(mkdocs_available, tmp_path):
    """Build into a tempdir so the repo working tree stays clean."""
    site_dir = tmp_path / "site"
    result = subprocess.run(
        [
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "mkdocs build --strict failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert (site_dir / "index.html").is_file(), "site/index.html not produced"
