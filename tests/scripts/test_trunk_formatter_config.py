"""[if] .trunk/trunk.yaml drifts from the ratified formatter pins or flags [then] this test fails, [else stop].

Part 9 of issue #4456 turns Trunk's formatters on. `trunk fmt` agrees byte for byte with the pinned
formatters only while trunk.yaml names the same versions and flags the repo pins elsewhere: shfmt in
scripts/format_proof_shell.py, ruff in ops/quality/requirements.txt, prettier and its svelte plugin in
apps/webui/frontend/package.json. Measured agreement: (private path) round 12.

Regression lines:
  - if trunk's shfmt version is not format_proof_shell's pin, or its run command is not shfmt -w -i 4 -ci, then broken
  - if trunk's ruff version is not ops/quality/requirements.txt's, or ruff format lacks --force-exclude, then broken
  - if trunk's prettier or prettier-plugin-svelte version is not the frontend's pin then broken
  - if trunk's prettier is not restricted to the frontend, run from there, then broken
  - if the trunk-fmt-pre-commit action (a rewriting git hook in the shared git dir) is enabled then broken
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.format_proof_shell import CFG as SHELL_CFG

pytestmark = [pytest.mark.requirement("OPS-8960c")]

ROOT = Path(__file__).resolve().parents[2]
TRUNK: dict[str, Any] = yaml.safe_load((ROOT / ".trunk/trunk.yaml").read_text())
FRONTEND_PINS: dict[str, str] = json.loads((ROOT / "apps/webui/frontend/package.json").read_text())["devDependencies"]


def _enabled(name: str) -> tuple[str, dict[str, Any]]:
    """(version, options) of an enabled linter entry `name@version` or `{name@version: options}`."""
    for entry in TRUNK["lint"]["enabled"]:
        key, options = (entry, {}) if isinstance(entry, str) else next(iter(entry.items()))
        if key.split("@")[0] == name:
            return key.split("@")[1], options
    raise AssertionError(f"{name} is not enabled in .trunk/trunk.yaml")


def _format_command(name: str) -> dict[str, Any]:
    definition = next(item for item in TRUNK["lint"]["definitions"] if item["name"] == name)
    return next(command for command in definition["commands"] if command["name"] == "format")


def test_shfmt_is_the_pinned_version_with_the_ratified_flags() -> None:
    version, _ = _enabled("shfmt")
    assert version == SHELL_CFG.SHFMT_VERSION
    assert _format_command("shfmt")["run"] == f"shfmt -w {' '.join(SHELL_CFG.SHFMT_FLAGS)} ${{target}}"


def test_ruff_format_is_on_at_the_quality_pin_and_honours_the_exclusions() -> None:
    version, options = _enabled("ruff")
    pin = next(
        line.split("==")[1]
        for line in (ROOT / "ops/quality/requirements.txt").read_text().splitlines()
        if line.startswith("ruff==")
    )
    assert version == pin
    assert "format" in options["commands"]
    assert _format_command("ruff")["run"] == "ruff format --force-exclude ${target}"


def test_prettier_matches_the_frontend_pins_and_scope() -> None:
    version, options = _enabled("prettier")
    assert version == FRONTEND_PINS["prettier"]
    assert options["packages"] == [f"prettier-plugin-svelte@{FRONTEND_PINS['prettier-plugin-svelte']}"]
    definition = next(item for item in TRUNK["lint"]["definitions"] if item["name"] == "prettier")
    assert definition["files"] == ["typescript", "javascript", "svelte"]
    assert _format_command("prettier")["run_from"] == "apps/webui/frontend"
    ignore = next(item for item in TRUNK["lint"]["ignore"] if item["linters"] == ["prettier"])
    assert ignore["paths"] == ["**", "!apps/webui/frontend/**"]


def test_no_formatter_is_disabled_again_and_the_rewriting_hook_stays_off() -> None:
    assert not {"shfmt", "prettier"} & set(TRUNK["lint"]["disabled"])
    assert "trunk-fmt-pre-commit" in TRUNK["actions"]["disabled"]
    assert "trunk-fmt-pre-commit" not in TRUNK["actions"].get("enabled", [])
