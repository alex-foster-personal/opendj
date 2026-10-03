"""Folder ingest must not load pyrekordbox at import time.

[if] folder ingest is imported in a clean process [then] pyrekordbox stays unloaded, [else stop].

Regression one-liners:
  - if importing folder ingest loads pyrekordbox then the onboarding path lies
  - if folder ingest imports the rekordbox adapter after pyrekordbox was
    deferred, a pyrekordbox-only probe stays green
  - if this test runs in-process then unrelated imports can hide the coupling
  - if the probe cannot see pyrekordbox at all then absence is not evidence
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("SETUP-03")

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# rekordbox_db defers pyrekordbox, so the package name alone no longer proves
# the folder module stayed off the rekordbox adapter.
_FORBIDDEN = (
    "pyrekordbox",
    "apps.shared.rekordbox_db",
    "apps.shared.state.ingest.rekordbox",
)


def _fresh(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-"],
        input=textwrap.dedent(source),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )


def _ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PROBE_OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_folder_ingest_import_does_not_load_pyrekordbox() -> None:
    """[if] folder ingest is imported fresh [then] pyrekordbox stays unloaded, [else stop]."""
    names = ", ".join(repr(name) for name in _FORBIDDEN)
    _ok(
        _fresh(
            f"""
            import sys

            import apps.shared.state.ingest.folder  # noqa: F401

            loaded = [name for name in ({names}) if name in sys.modules]
            assert not loaded, loaded
            print("PROBE_OK")
            """
        )
    )


def test_pyrekordbox_probe_sees_a_real_import() -> None:
    """[if] a clean process imports pyrekordbox [then] the probe sees it, [else stop]."""
    _ok(
        _fresh(
            """
            import sys

            import pyrekordbox  # noqa: F401

            assert "pyrekordbox" in sys.modules
            print("PROBE_OK")
            """
        )
    )
