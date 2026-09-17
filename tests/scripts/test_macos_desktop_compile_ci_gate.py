"""DEVOPS-12 keeps the desktop shell's macOS-only Rust paths in a compile gate.

[if] a desktop shell change lands [then] the macOS workflow runs cargo check on it, [else stop].

Acceptance:
  - [if] a desktop shell change lands [then] the macOS workflow runs cargo check
    against opendj-desktop, [else stop].
  - [if] a PR touches only Python or Svelte paths [then] the macOS desktop compile
    workflow does not trigger, [else stop].
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/macos-desktop-compile.yml"
pytestmark = pytest.mark.requirement("DEVOPS-12")


def test_macos_desktop_compile_workflow_runs_cargo_check_for_opendj_desktop() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked --bin opendj-desktop" in workflow
    assert "mkdir -p apps/desktop/src-tauri/payload" in workflow
    assert "scripts/ci_cargo_selfheal.sh apps/desktop/src-tauri/Cargo.toml dev" in workflow


def test_macos_desktop_compile_workflow_triggers_only_on_desktop_paths() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = document[True] if True in document else document["on"]
    expected_paths = {
        "apps/desktop/**",
        ".github/workflows/macos-desktop-compile.yml",
    }

    assert set(triggers["push"]["paths"]) == expected_paths
    assert set(triggers["pull_request"]["paths"]) == expected_paths
