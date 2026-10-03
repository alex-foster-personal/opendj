"""OSSPUB-06: rbox is fetched on first use and is not a payload dependency."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from apps.sync.usb.pioneer.rbox_runtime import ensure_rbox
from scripts.build_engine_payload import _requirement_name, locked_requirements

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.requirement("OSSPUB-06")
def test_rbox_is_absent_from_core_and_payload_dependencies() -> None:
    """[if] rbox is a core or payload dependency [then] the DMG ships GPLv3, [else stop]."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core_names = {_requirement_name(req) for req in pyproject["project"]["dependencies"]}
    assert "rbox" not in core_names
    entries, _dropped = locked_requirements(REPO_ROOT)
    assert "rbox" not in {entry.name for entry in entries}


@pytest.mark.requirement("OSSPUB-06")
def test_ensure_rbox_returns_the_installed_module() -> None:
    """[if] the dev extra installed rbox [then] ensure_rbox returns that module, [else stop]."""
    module = ensure_rbox()
    assert module.__name__ == "rbox"
    assert callable(module.OneLibrary)
