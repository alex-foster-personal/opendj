"""Parse SECURITY.md supported-version rows against shipped app release metadata."""

from __future__ import annotations

import re
from pathlib import Path

from apps.shared import app_version
from scripts.release_identity import release_tag

SECTION_HEADING = "## Supported versions"
CURRENT_STATUS_PHRASE = "current public release"
APP_V_IN_CELL = re.compile(r"app-v([^`) \]]+)", re.IGNORECASE)


class SupportedVersionDrift(Exception):
    """SECURITY.md Supported versions disagrees with release metadata."""


def supported_versions_section(security_text: str) -> str:
    """Body under ``## Supported versions`` through the next ``##`` heading or EOF."""
    start = security_text.find(SECTION_HEADING)
    if start < 0:
        return ""
    body = security_text[start + len(SECTION_HEADING) :]
    next_heading = body.find("\n## ")
    if next_heading >= 0:
        body = body[:next_heading]
    return body


def current_public_release_tag(security_text: str) -> str | None:
    """``app-v<semver>`` from the row whose status is the current public release."""
    for line in supported_versions_section(security_text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("|:"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if CURRENT_STATUS_PHRASE not in cells[1].lower():
            continue
        match = APP_V_IN_CELL.search(cells[0])
        if match:
            return release_tag(match.group(1))
    return None


def configured_release_tag(repo_root: Path) -> str:
    """Shipping tag ``app-v<semver>`` from ``apps/desktop/electron/package.json``."""
    version = app_version.resolve_app_version({}, repo_root)
    if version == app_version.UNKNOWN_VERSION:
        raise SupportedVersionDrift("release metadata has no readable app version")
    return release_tag(version)


def assert_supported_versions_current(security_text: str, repo_root: Path) -> None:
    """Fail when SECURITY.md lacks or misnames the current supported release."""
    stated = current_public_release_tag(security_text)
    if stated is None:
        raise SupportedVersionDrift(f"{SECTION_HEADING} has no row marked {CURRENT_STATUS_PHRASE!r} with an app-v tag")
    expected = configured_release_tag(repo_root)
    if stated != expected:
        raise SupportedVersionDrift(
            f"SECURITY.md names {stated!r} as the current release; "
            f"release metadata is {expected!r} ({app_version.APP_PACKAGE_REL.as_posix()})"
        )
