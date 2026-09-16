"""LAUNCH-01 keeps the Hyper-K launcher's macOS packaging path in CI.

[if] a launcher packaging change lands [then] the macOS workflow runs just launcher-build, [else stop].

Acceptance:
  - [if] a launcher tree change lands [then] the macOS workflow runs just launcher-build, [else stop].
  - [if] a PR touches only unrelated paths [then] the launcher-packaging job does not trigger, [else stop].
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/macos-packaging.yml"
pytestmark = pytest.mark.requirement("LAUNCH-01")


def test_macos_launcher_packaging_workflow_runs_just_launcher_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "launcher-packaging:" in workflow
    assert "just launcher-build" in workflow


def test_macos_launcher_packaging_workflow_triggers_on_launcher_paths() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = document[True] if True in document else document["on"]
    paths = set(triggers["push"]["paths"])

    assert "apps/launcher/**" in paths
    assert set(triggers["pull_request"]["paths"]) == paths
