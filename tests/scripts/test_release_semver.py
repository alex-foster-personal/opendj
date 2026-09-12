"""Semver guard contract for just release."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release_semver import (
    parse_semver,
    read_configured_version,
    require_semver_bump,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONF = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"


def test_a_strictly_higher_version_passes() -> None:
    require_semver_bump("0.1.1", "0.1.0")


def test_an_equal_version_is_refused() -> None:
    with pytest.raises(ValueError, match="semver bump"):
        require_semver_bump("0.1.0", "0.1.0")


def test_a_lower_version_is_refused() -> None:
    with pytest.raises(ValueError, match="semver bump"):
        require_semver_bump("0.1.0", "0.1.1")


def test_the_first_release_passes_with_no_published_version() -> None:
    require_semver_bump("0.1.0", None)


def test_a_leading_v_on_either_side_is_tolerated() -> None:
    require_semver_bump("v0.2.0", "v0.1.9")


def test_the_configured_version_is_read_from_tauri_conf() -> None:
    configured = read_configured_version(str(CONF))
    parsed = parse_semver(configured, source="configured")
    assert parsed.major >= 0


def test_release_publisher_runs_the_semver_guard_before_building() -> None:
    publisher = (REPO_ROOT / "scripts/release.sh").read_text(encoding="utf-8")
    assert "python3 -m scripts.release_semver check" in publisher
    assert publisher.index("python3 -m scripts.release_semver check") < publisher.index("just dmg")


def test_release_recipe_exposes_the_agent_native_semver_check() -> None:
    recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert "release-check-semver:" in recipe
    assert "python -m scripts.release_semver check" in recipe


def test_the_cli_refuses_an_unchanged_version() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.release_semver",
            "check",
            "--configured",
            "0.1.0",
            "--published",
            "0.1.0",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "semver bump" in result.stderr
