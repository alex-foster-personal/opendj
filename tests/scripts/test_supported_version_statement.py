"""SECURITY.md Supported versions must track shipped app release metadata.

[if] SECURITY.md drifts from release metadata [then] a test fails, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared import app_version
from scripts import supported_version_statement as svs

pytestmark = pytest.mark.requirement("PLATFORM-14458")

REPO = Path(__file__).resolve().parents[2]
SECURITY_MD = REPO / "SECURITY.md"


def _table(*rows: str) -> str:
    header = (
        f"{svs.SECTION_HEADING}\n\n| Version | Status | Security updates |\n|:--------|:-------|:-----------------|\n"
    )
    return header + "\n".join(rows) + "\n"


def test_live_security_policy_matches_release_metadata() -> None:
    """[if] supported table names different current release [then] test fails, [else stop]."""
    svs.assert_supported_versions_current(SECURITY_MD.read_text(encoding="utf-8"), REPO)


def test_project_version_change_without_security_update_fails(tmp_path: Path) -> None:
    """[if] the project version changes [then] drift fails, [else stop]."""
    pkg = tmp_path / app_version.APP_PACKAGE_REL
    pkg.parent.mkdir(parents=True)
    pkg.write_text(json.dumps({"version": "2.0.0-beta.1"}), encoding="utf-8")
    security = _table("| 1.0 alpha (`app-v1.0.0-alpha.2`) | Current public release | Yes |")
    with pytest.raises(svs.SupportedVersionDrift, match="release metadata is 'app-v2.0.0-beta.1'"):
        svs.assert_supported_versions_current(security, tmp_path)


def test_security_policy_without_current_release_row_fails() -> None:
    """[if] security policy has no current supported release row [then] test fails, [else stop]."""
    security = _table("| 1.0 alpha (`app-v1.0.0-alpha.2`) | Previous public release | Yes |")
    with pytest.raises(svs.SupportedVersionDrift, match="no row marked"):
        svs.assert_supported_versions_current(security, REPO)
