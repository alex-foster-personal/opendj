"""Store requirements that live in the Tauri config, pinned.

These are not a test of the publishing pipeline, which is deliberately out of
CI (see .agents/skills/ship-appstore/SKILL.md). They pin the two facts in
``tauri.conf.json`` that App Store Connect refuses a build over, because both
are one-line reverts away and neither failure is visible until an upload is
rejected 20 minutes into a release.

  [if] bundle.category is removed, App Store Connect rejects the build for a
       missing LSApplicationCategoryType -> test_category_is_declared
  [if] minimumSystemVersion drops below 11.0 while the product ships arm64
       only, the store offers the app to Intel Macs that cannot launch it
       -> test_minimum_system_version_matches_the_shipped_architecture
  [if] the entitlements templates are deleted or rendered unusable, the
       signing phase cannot produce a sandboxed bundle
       -> test_entitlements_templates_are_present_and_are_templates
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

TAURI_DIR = Path(__file__).resolve().parents[2] / "apps/desktop/src-tauri"
CONF = TAURI_DIR / "tauri.conf.json"


@pytest.fixture(scope="module")
def bundle() -> dict:
    return json.loads(CONF.read_text())["bundle"]


def test_category_is_declared(bundle: dict) -> None:
    """LSApplicationCategoryType is mandatory for a store submission."""
    assert bundle.get("category"), (
        "bundle.category is missing from tauri.conf.json. Tauri maps it to "
        "LSApplicationCategoryType, which App Store Connect requires; a build "
        "without it is rejected at upload."
    )


def test_minimum_system_version_matches_the_shipped_architecture(
    bundle: dict,
) -> None:
    """arm64 did not exist before macOS 11, so 10.x advertises a lie."""
    raw = bundle["macOS"]["minimumSystemVersion"]
    major = int(str(raw).split(".")[0])
    assert major >= 11, (
        f"minimumSystemVersion is {raw}, but the product ships arm64 only "
        "(the dmg recipe passes no --target, so it builds for the host). "
        "Anything below 11.0 offers the app to Intel Macs that cannot run it. "
        "Either raise this, or start building universal binaries."
    )


@pytest.mark.parametrize(
    ("name", "expected_key"),
    [
        ("Entitlements.appstore.template.plist", "com.apple.security.app-sandbox"),
        ("Entitlements.appstore.inherit.template.plist", "com.apple.security.inherit"),
    ],
)
def test_entitlements_templates_are_present_and_are_templates(
    name: str, expected_key: str
) -> None:
    """Both templates parse as plists and carry their defining key.

    Parsed rather than grepped so a malformed plist fails here instead of at
    ``codesign``, where the error is considerably less clear.
    """
    path = TAURI_DIR / name
    assert path.is_file(), f"{name} is missing; the signing phase needs it"
    data = plistlib.loads(path.read_bytes())
    assert data.get(expected_key) is True, f"{name} must set {expected_key}"


def test_the_inherit_template_carries_exactly_one_entitlement() -> None:
    """macOS rejects a binary that pairs com.apple.security.inherit with anything.

    Worth its own test because the tempting mistake (adding network.server to
    the engine, which is the process that actually binds the port) produces an
    INVALID signature rather than a more permissive one.
    """
    data = plistlib.loads(
        (TAURI_DIR / "Entitlements.appstore.inherit.template.plist").read_bytes()
    )
    assert list(data) == ["com.apple.security.inherit"], (
        "the inherited-sandbox template must carry com.apple.security.inherit "
        f"and nothing else, got {sorted(data)}. A child of a sandboxed app "
        "receives the parent's entitlements through inheritance; declaring "
        "them again makes the signature invalid."
    )
