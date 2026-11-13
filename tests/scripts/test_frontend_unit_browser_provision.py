"""The frontend unit gate provisions Chromium for browser-backed unit tests (#12781)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "apps" / "webui" / "frontend"
PACKAGE_JSON = FRONTEND / "package.json"


def _scripts() -> dict[str, str]:
    """Return the frontend package.json scripts table."""
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]


def test_ensure_unit_browsers_script_exists() -> None:
    """The ensure:unit-browsers script points at the provisioning helper."""
    assert _scripts()["ensure:unit-browsers"] == "node scripts/ensure-unit-browsers.mjs"
    assert (FRONTEND / "scripts" / "ensure-unit-browsers.mjs").is_file()


@pytest.mark.parametrize("name", ["test:unit", "test:unit:all"])
def test_unit_runs_provision_chromium_before_node_test(name: str) -> None:
    """Every unit run that globs tests/unit/*.test.mjs provisions Chromium before node --test."""
    script = _scripts()[name]
    assert "pnpm run ensure:unit-browsers" in script
    assert script.index("pnpm run ensure:unit-browsers") < script.index("node --experimental-strip-types --test")
