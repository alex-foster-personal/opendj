"""Folder ingest must not load pyrekordbox at import time.

[if] folder ingest is imported in a clean process [then] pyrekordbox stays unloaded, [else stop].

Regression one-liners:
  - if importing folder ingest loads pyrekordbox then the onboarding path lies
  - if this test runs in-process then unrelated imports can hide the coupling
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("SETUP-03")

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_folder_ingest_import_does_not_load_pyrekordbox() -> None:
    """[if] folder ingest is imported fresh [then] pyrekordbox stays unloaded, [else stop]."""
    probe = textwrap.dedent(
        """
            import sys

            import apps.shared.state.ingest.folder  # noqa: F401

            assert "pyrekordbox" not in sys.modules
            print("PROBE_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-"],
        input=probe,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PROBE_OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr}"
