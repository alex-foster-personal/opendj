"""OSSPUB-06: rbox is not a core or payload dependency, so the DMG never ships GPLv3.

Moved from tests/sync/usb/test_rbox_first_use.py: it reads the payload builder in
scripts/, so it is a scripts suite (SMARTEST-CI round 16).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.build_engine_payload import _requirement_name, locked_requirements

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.requirement("OSSPUB-06")
def test_rbox_is_absent_from_core_and_payload_dependencies() -> None:
    """[if] rbox is a core or payload dependency [then] the DMG ships GPLv3, [else stop]."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core_names = {_requirement_name(req) for req in pyproject["project"]["dependencies"]}
    assert "rbox" not in core_names
    entries, _dropped = locked_requirements(REPO_ROOT)
    assert "rbox" not in {entry.name for entry in entries}
