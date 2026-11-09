"""If mcp_support fails at import without the MCP SDK then unrelated CLI/status tests stop collecting."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = """
import sys
sys.modules["mcp"] = None  # hide the SDK: any import of it raises ImportError
import pytest
from tests.opendj_cli import mcp_support
assert mcp_support.closed_gate_env()["OPENDJ_MCP_ENABLE_DESTRUCTIVE"] == ""
try:
    mcp_support.stdio_params(None)
except pytest.skip.Exception as exc:
    assert "UNAVAILABLE" in str(exc), exc
    print("SKIPPED")
else:
    raise AssertionError("stdio_params ran without the MCP SDK")
"""


def test_helper_imports_and_skips_per_call_without_the_mcp_sdk() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "SKIPPED", proc.stdout
