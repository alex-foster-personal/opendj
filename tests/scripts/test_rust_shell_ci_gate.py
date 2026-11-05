"""DEVLOOP-05 keeps the desktop shell's Rust tests in the pull-request gate.

Acceptance:
  - [if] a shell change lands [then] the pull-request gate ran cargo test against it, [else stop].
  - [if] the CoreAudio helper crate's tests run only through the Tauri manifest [then] deleting
    apps/desktop/src-tauri drops them from the gate, [else stop].
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.requirement("DEVLOOP-05")


def test_ci_gate_runs_the_desktop_shell_rust_tests() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "mkdir -p apps/desktop/src-tauri/payload" in workflow
        and "cargo nextest run --manifest-path apps/desktop/src-tauri/Cargo.toml --locked"
        in workflow
    )
    # The CoreAudio probe and cue sink tests live in their own crate since the
    # Tauri deletion prep; the shell step must still run them.
    assert "-p opendj-desktop -p odj-output-probe" in workflow


def test_ci_gate_runs_the_output_probe_tests_on_the_probe_crate_manifest() -> None:
    """[if] ci.yml runs the probe tests only via the Tauri manifest [then] fail, [else stop]."""
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    run = "cargo nextest run --manifest-path apps/desktop/native/odj-output-probe/Cargo.toml --locked"
    assert run in workflow
    # Its exit code must reach the step's verdict, not be dropped after `set -e` returns.
    step = workflow.split(run, 1)[1].split("set -e", 1)[0]
    assert "probe_rc=$?" in step and "rc=$probe_rc" in step
