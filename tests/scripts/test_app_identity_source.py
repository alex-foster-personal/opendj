"""The app's name, identifier and version have one source (ELECTRON-8791).

[if] the build reads identity from tauri.conf.json [then] Tauri deletion breaks it, [else stop].
[if] the Tauri copy drifts from the app package [then] the shells ship two identities, [else stop].
[if] the app package lacks a field [then] the build stops and names it, [else stop].

-Claude
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

from apps.shared import app_version
from scripts import build_engine_payload, build_engine_payload_win
from scripts.app_identity import APP_PACKAGE_REL, AppIdentityError, read_app_identity

pytestmark = pytest.mark.requirement("ELECTRON-8791")

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_CONF = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"


def test_the_identity_comes_from_the_app_package() -> None:
    package = json.loads((REPO_ROOT / APP_PACKAGE_REL).read_text(encoding="utf-8"))
    identity = read_app_identity(REPO_ROOT)
    assert identity.product_name == package["productName"]
    assert identity.identifier == package["opendj"]["identifier"]
    assert identity.version == package["version"]
    assert app_version.APP_PACKAGE_REL.as_posix() == APP_PACKAGE_REL


def test_the_tauri_copy_matches_while_the_tauri_shell_ships() -> None:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    identity = read_app_identity(REPO_ROOT)
    assert (conf["productName"], conf["identifier"], conf["version"]) == (
        identity.product_name,
        identity.identifier,
        identity.version,
    ), f"bump {TAURI_CONF.relative_to(REPO_ROOT)} together with {APP_PACKAGE_REL}"


@pytest.mark.parametrize("module", [build_engine_payload, build_engine_payload_win])
def test_the_payload_builders_no_longer_read_tauri_conf(module: ModuleType) -> None:
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert 'tauri.conf.json"' not in source and "tauri.conf.json')" not in source
    assert "src-tauri/tauri.conf.json" not in source


@pytest.mark.parametrize(
    ("package", "field"),
    [
        ({"productName": "Open DJ", "opendj": {"identifier": "com.opendj.desktop"}}, "version"),
        ({"version": "1.0.0", "opendj": {"identifier": "com.opendj.desktop"}}, "productName"),
        ({"version": "1.0.0", "productName": "Open DJ"}, "opendj.identifier"),
        ({"version": " ", "productName": "Open DJ", "opendj": {"identifier": "x"}}, "version"),
    ],
)
def test_a_package_missing_a_field_stops_the_build(tmp_path: Path, package: object, field: str) -> None:
    path = tmp_path / APP_PACKAGE_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(AppIdentityError, match=field.replace(".", r"\.")):
        read_app_identity(tmp_path)


def test_control_a_complete_package_is_read(tmp_path: Path) -> None:
    path = tmp_path / APP_PACKAGE_REL
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"version": "9.9.9-beta.3", "productName": "X", "opendj": {"identifier": "com.example.x"}}),
        encoding="utf-8",
    )
    identity = read_app_identity(tmp_path)
    assert (identity.product_name, identity.identifier, identity.version) == ("X", "com.example.x", "9.9.9-beta.3")
