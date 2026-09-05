"""DEVLOOP-05 keeps the desktop shell's Rust tests in the pull-request gate.

Acceptance:
  - [if] a shell change lands [then] a Rust test ran against it in the gate
    broken
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.requirement("DEVLOOP-05")


def test_ci_gate_runs_the_desktop_shell_rust_tests() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "mkdir -p apps/desktop/src-tauri/payload" in workflow
        and "cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked" in workflow
    )
