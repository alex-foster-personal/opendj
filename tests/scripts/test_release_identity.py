"""[if] app release identity is constructed [then] tag and URL agree, [else stop]."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.engine_core.build_info import resolve_build_info
from scripts.release_identity import release_asset_url, release_tag

pytestmark = pytest.mark.requirement("OPS-15")
ROOT = Path(__file__).resolve().parents[2]
CONF = ROOT / "apps/desktop/src-tauri/tauri.conf.json"


def test_shipping_identity_uses_the_canonical_launch_version() -> None:
    version = json.loads(CONF.read_text(encoding="utf-8"))["version"]
    assert version == "1.0.0-alpha.1"
    assert resolve_build_info({}, ROOT).app_version == version
    assert release_tag(version) == "app-v1.0.0-alpha.1"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.release_identity", "--config", str(CONF)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == release_tag(version)


@pytest.mark.parametrize("version", ["1.0.0-alpha.1", "1.0.0-beta.1", "1.0.0"])
def test_public_download_url_names_the_same_tag(version: str) -> None:
    assert release_asset_url("owner/assets", version, "OpenDJ.app.tar.gz") == (
        f"https://github.com/owner/assets/releases/download/{release_tag(version)}/OpenDJ.app.tar.gz"
    )


def test_asset_url_encodes_metadata_and_names() -> None:
    assert release_asset_url("owner/assets", "1.0.0+build.1", "Open DJ.tar.gz").endswith(
        "/app-v1.0.0%2Bbuild.1/Open%20DJ.tar.gz"
    )


@pytest.mark.parametrize("version", ["v1.0.0", " 1.0.0", "1.0.0 ", "1.0", "garbage"])
def test_prefixed_or_invalid_config_versions_fail_explicitly(version: str) -> None:
    with pytest.raises(ValueError):
        release_tag(version)
