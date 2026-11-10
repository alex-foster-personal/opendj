"""Lock the documented Semgrep install-set overrides for mcp 1.30.0."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "scripts" / "security" / "semgrep-requirements.txt"

MCP_GHSAS = (
    "GHSA-5h93-6whr-6q8j",
    "GHSA-84m7-p3x7-pcfv",
    "GHSA-rwrf-2pqf-9j8j",
    "GHSA-w4fh-qvv9-3v23",
)


@pytest.fixture(scope="module")
def manifest_text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def test_header_override_pins_mcp_1_30_0(manifest_text: str) -> None:
    header = manifest_text.split("annotated-types==", 1)[0]
    assert "mcp==1.30.0" in header
    installed = re.search(r"^semgrep==(\S+) \\$", manifest_text, flags=re.M)
    assert installed, "if the install set has no semgrep pin then the manifest is broken"
    assert f"semgrep=={installed.group(1)}" in header, (
        "if the header recipe names another semgrep than the install set then the doc is stale"
    )


def test_install_set_pins_mcp_1_30_0(manifest_text: str) -> None:
    assert "\nmcp==1.30.0 \\\n" in manifest_text


def test_header_cites_upstream_advisories_and_issues(manifest_text: str) -> None:
    header = manifest_text.split("annotated-types==", 1)[0]
    for ghsa in MCP_GHSAS:
        assert ghsa in header, f"missing {ghsa} in header"
    assert "GHSA-xcq7-p3pf-p88h" in header
    assert "11992" in header
    assert "12018" in header


def test_via_comments_use_documented_tmp_paths(manifest_text: str) -> None:
    assert "/tmp/t5-" not in manifest_text
    assert "/tmp/semgrep-overrides.txt" in manifest_text
    assert "/tmp/semgrep.in" in manifest_text
