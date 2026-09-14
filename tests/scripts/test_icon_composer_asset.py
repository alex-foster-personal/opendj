"""macOS 26 Icon Composer asset compile/install (INSTALL-20, issue #2566)."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest
from PIL import Image

from scripts.build_engine_payload import PayloadBuildError
from scripts.icon_composer_asset import (
    DEFAULT_ICON_SOURCE,
    ICON_SET_NAME,
    compile_icon_composer_asset,
    find_actool,
    install_into_app_bundle,
    verify_icon_composer_source,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _icon_source(tmp_path: Path, fill: str, layer_png: Image.Image | None) -> Path:
    source = tmp_path / "AppIcon.icon"
    (source / "Assets").mkdir(parents=True)
    spec = {
        "fill": {"solid": fill},
        "groups": [
            {
                "layers": [{"glass": False, "image-name": "Mark.png", "name": "Mark"}],
                "shadow": {"kind": "none", "opacity": 0.0},
                "translucency": {"enabled": False, "value": 0.0},
            }
        ],
        "supported-platforms": {"squares": "shared"},
    }
    (source / "icon.json").write_text(json.dumps(spec))
    if layer_png is not None:
        layer_png.save(source / "Assets" / "Mark.png")
    return source


@pytest.mark.requirement("INSTALL-20")
def test_committed_icon_composer_source_is_valid() -> None:
    """[if] the committed .icon doc is malformed/mismatched [then] verify raises, [else stop]."""
    verify_icon_composer_source(DEFAULT_ICON_SOURCE)


@pytest.mark.requirement("INSTALL-20")
def test_fill_mismatch_is_refused(tmp_path: Path) -> None:
    """[if] icon.json fill diverges from the tile color [then] verify raises, [else stop]."""
    mark = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    mark.paste((217, 119, 87, 255), (64, 64, 192, 192))
    source = _icon_source(tmp_path, "extended-srgb:1.00000,0.00000,0.00000,1.00000", mark)
    with pytest.raises(PayloadBuildError, match="fill"):
        verify_icon_composer_source(source)


@pytest.mark.requirement("INSTALL-20")
def test_fully_transparent_layer_is_refused(tmp_path: Path) -> None:
    """[if] the mark layer PNG is fully transparent [then] verify raises, [else stop]."""
    blank = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    source = _icon_source(tmp_path, "extended-srgb:0.05098,0.05882,0.07059,1.00000", blank)
    with pytest.raises(PayloadBuildError, match="transparent"):
        verify_icon_composer_source(source)


@pytest.mark.requirement("INSTALL-20")
def test_missing_layer_asset_is_refused(tmp_path: Path) -> None:
    """[if] icon.json references a missing layer PNG [then] verify raises, [else stop]."""
    source = _icon_source(tmp_path, "extended-srgb:0.05098,0.05882,0.07059,1.00000", None)
    with pytest.raises(PayloadBuildError, match="missing layer asset"):
        verify_icon_composer_source(source)


@pytest.mark.requirement("INSTALL-20")
def test_actool_absent_without_full_xcode_names_the_remedy() -> None:
    """[if] actool is not resolvable (CLT-only host) [then] it raises naming Xcode, [else stop].

    This host has no full Xcode install (confirmed: xcrun --find actool fails
    under CommandLineTools), so this exercises the real refusal path rather
    than a mocked one. On a host WITH Xcode this test is a no-op pass-through
    (find_actool succeeds), which is the correct behavior either way.
    """
    try:
        find_actool()
    except PayloadBuildError as exc:
        assert "Xcode" in str(exc)
    else:
        pytest.skip("this host has a full Xcode install; nothing to refuse")


@pytest.mark.requirement("INSTALL-20")
def test_compile_without_actool_is_refused(tmp_path: Path) -> None:
    """[if] compiling without actool [then] it raises before touching Assets.car, [else stop]."""
    try:
        find_actool()
    except PayloadBuildError:
        pass
    else:
        pytest.skip("this host has actool; the no-actool path is not exercised here")

    with pytest.raises(PayloadBuildError, match="Xcode"):
        compile_icon_composer_asset(DEFAULT_ICON_SOURCE, out_dir=tmp_path / "out")


@pytest.mark.requirement("INSTALL-20")
def test_install_sets_icon_name_without_disturbing_other_keys(tmp_path: Path) -> None:
    """[if] installing where CFBundleIconName is absent [then] only that key changes, [else stop]."""
    app = tmp_path / "Open DJ.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    original = {
        "CFBundleIconFile": "icon.icns",
        "CFBundleIdentifier": "app.opendj.desktop",
        "CFBundleExecutable": "opendj-desktop",
    }
    info_plist = app / "Contents" / "Info.plist"
    with info_plist.open("wb") as fh:
        plistlib.dump(original, fh)

    fake_assets_car = tmp_path / "Assets.car"
    fake_assets_car.write_bytes(b"fake-compiled-asset-catalog")

    install_into_app_bundle(app, fake_assets_car)

    assert (
        app / "Contents" / "Resources" / "Assets.car"
    ).read_bytes() == b"fake-compiled-asset-catalog"
    with info_plist.open("rb") as fh:
        updated = plistlib.load(fh)
    assert updated["CFBundleIconName"] == ICON_SET_NAME
    for key, value in original.items():
        assert updated[key] == value


@pytest.mark.requirement("INSTALL-20")
def test_install_refuses_a_path_that_is_not_a_built_app(tmp_path: Path) -> None:
    """[if] app_path has no Contents/Resources [then] install raises, [else stop]."""
    with pytest.raises(PayloadBuildError, match="Resources"):
        install_into_app_bundle(tmp_path / "not-an-app", tmp_path / "Assets.car")
