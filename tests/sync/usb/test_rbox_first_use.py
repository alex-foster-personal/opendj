"""OSSPUB-06: rbox is fetched on first use and is not a payload dependency."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.pioneer.rbox_runtime import ensure_rbox

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.requirement("OSSPUB-06")
def test_ensure_rbox_returns_the_installed_module() -> None:
    """[if] the dev extra installed rbox [then] ensure_rbox returns that module, [else stop]."""
    module = ensure_rbox()
    assert module.__name__ == "rbox"
    assert callable(module.OneLibrary)
