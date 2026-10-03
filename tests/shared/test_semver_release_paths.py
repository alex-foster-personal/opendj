"""[if] release versions advance by SemVer precedence [then] both paths agree, [else stop]."""

from __future__ import annotations

import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

from apps.engine_core.update_channel import UpdateCheckError, compare_versions
from scripts.release_semver import parse_semver, require_semver_bump

pytestmark = pytest.mark.requirement("OPS-15")

ORDERED = (
    "0.1.5",
    "1.0.0-0",
    "1.0.0-9",
    "1.0.0-10",
    "1.0.0-ALPHA",
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.2",
    "1.0.0-alpha.10",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.1",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
    "1.0.1-alpha.1",
    "1.0.1",
    "1.1.0",
    "2.0.0",
)


@pytest.mark.parametrize(("older", "newer"), tuple(pairwise(ORDERED)))
def test_both_production_paths_order_release_progression(older: str, newer: str) -> None:
    """Every forward edge is offered; the reverse edge cannot be published."""
    require_semver_bump(newer, older)
    assert compare_versions(older, newer) == "update-available"
    with pytest.raises(ValueError, match="semver bump"):
        require_semver_bump(older, newer)
    assert compare_versions(newer, older) == "ahead-of-channel"


@pytest.mark.parametrize("version", ORDERED)
def test_equal_versions_never_publish_or_offer_an_update(version: str) -> None:
    with pytest.raises(ValueError, match="semver bump"):
        require_semver_bump(version, version)
    assert compare_versions(version, version) == "up-to-date"


@pytest.mark.parametrize("core", ("1.0.0", "1.0.0-alpha.1"))
def test_build_metadata_and_leading_v_preserve_precedence(core: str) -> None:
    left = f"v{core}+build.001"
    right = f"{core}+different.002"
    with pytest.raises(ValueError, match="semver bump"):
        require_semver_bump(left, right)
    assert compare_versions(left, right) == "up-to-date"


@pytest.mark.parametrize(
    "invalid",
    ("1.0.0-alpha.01", "1.0.0-01", "1.0.0-alpha..1", "1.0.0-", "1.0.0+", "1.0.0+a..b", "1.0.0-alpha_1", "01.0.0"),
)
def test_malformed_versions_remain_explicit_faults(invalid: str) -> None:
    with pytest.raises(ValueError, match="semver"):
        require_semver_bump(invalid, None)
    with pytest.raises(UpdateCheckError) as error:
        compare_versions("0.1.5", invalid)
    assert error.value.status == "manifest-malformed"


def test_app_tag_namespace_is_accepted_by_the_release_guard() -> None:
    require_semver_bump("1.0.0-alpha.2", "app-v1.0.0-alpha.1")
    assert parse_semver("app-v1.0.0-alpha.1", source="published").major == 1


def test_release_cli_accepts_prerelease_increment_and_refuses_reverse() -> None:
    root = Path(__file__).resolve().parents[2]
    for configured, published, expected in (
        ("1.0.0-alpha.2", "app-v1.0.0-alpha.1", 0),
        ("1.0.0", "1.0.0-beta.1", 0),
        ("1.0.0-alpha.1", "1.0.0-alpha.2", 1),
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.release_semver",
                "check",
                "--configured",
                configured,
                "--published",
                published,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == expected, result.stdout + result.stderr
