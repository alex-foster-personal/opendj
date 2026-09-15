"""macOS 26 Icon Composer asset compile/install (INSTALL-20, issue #2566, #2633)."""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from scripts.build_engine_payload import PayloadBuildError
from scripts.icon_composer_asset import (
    DEFAULT_COMMITTED_ASSETS_CAR,
    DEFAULT_ICON_SOURCE,
    DEFAULT_SOURCE_SHA256_SIDECAR,
    ICON_SET_NAME,
    IconAssetMode,
    check_icon_composer_asset,
    compile_icon_composer_asset,
    compute_source_digest,
    find_actool,
    install_into_app_bundle,
    prepare_and_install,
    read_source_sha256_sidecar,
    resolve_icon_asset_strategy,
    try_find_actool,
    verify_icon_composer_source,
    write_source_sha256_sidecar,
)
from tests.scripts.icon_composer_harness import require_no_actool_env, run_icon_composer_cli

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


@pytest.mark.requirement("INSTALL-20")
def test_committed_catalog_and_sidecar_match_the_committed_source() -> None:
    """[if] the committed fallback is present [then] its digest matches the source, [else stop]."""
    if not DEFAULT_COMMITTED_ASSETS_CAR.is_file() or not DEFAULT_SOURCE_SHA256_SIDECAR.is_file():
        pytest.skip("UNAVAILABLE: committed Assets.car fallback is not present in this checkout")
    digest = compute_source_digest(DEFAULT_ICON_SOURCE)
    assert read_source_sha256_sidecar(DEFAULT_SOURCE_SHA256_SIDECAR) == digest
    assert DEFAULT_COMMITTED_ASSETS_CAR.stat().st_size > 0


@pytest.mark.requirement("INSTALL-20")
def test_check_reports_compile_when_actool_is_available() -> None:
    """[if] actool is available [then] the read-only check selects compile, [else stop]."""
    if try_find_actool() is None:
        pytest.skip("UNAVAILABLE: actool is not on this host")
    line = check_icon_composer_asset()
    assert line.startswith("compile actool="), line
    assert "source-sha256=" in line, line


@pytest.mark.requirement("INSTALL-20")
def test_check_reports_reuse_when_actool_is_absent_and_fallback_matches(
    tmp_path: Path,
) -> None:
    """[if] actool is absent and the fallback matches [then] reuse is selected, [else stop]."""
    try:
        cap = require_no_actool_env()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    source = tmp_path / "AppIcon.icon"
    shutil.copytree(DEFAULT_ICON_SOURCE, source)
    committed = source / "Assets.car"
    sidecar = source / "Assets.car.source.sha256"
    if not committed.is_file():
        committed.write_bytes(b"committed-catalog")
    digest = compute_source_digest(source)
    write_source_sha256_sidecar(sidecar, digest, source=source)
    if cap == "native":
        line = check_icon_composer_asset(source, committed_car=committed, sidecar=sidecar)
        assert line.startswith("reuse-committed path="), line
        return
    proc = run_icon_composer_cli(
        "--check",
        "--source",
        str(source),
        "--committed-car",
        str(committed),
        "--sidecar",
        str(sidecar),
        cwd=REPO_ROOT,
        no_actool=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("reuse-committed path="), proc.stdout


@pytest.mark.requirement("INSTALL-20")
def test_stale_source_is_refused_when_actool_is_absent(tmp_path: Path) -> None:
    """[if] the source changes without actool [then] stale provenance is refused, [else stop]."""
    try:
        cap = require_no_actool_env()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    source = tmp_path / "AppIcon.icon"
    shutil.copytree(DEFAULT_ICON_SOURCE, source)
    committed = source / "Assets.car"
    sidecar = source / "Assets.car.source.sha256"
    committed.write_bytes(b"committed-catalog")
    write_source_sha256_sidecar(sidecar, "stale-digest", source=source)
    icon_json = source / "icon.json"
    spec = json.loads(icon_json.read_text())
    spec["groups"][0]["layers"][0]["name"] = "Changed"
    icon_json.write_text(json.dumps(spec))
    if cap == "native":
        with pytest.raises(PayloadBuildError, match="stale or unprovenanced"):
            resolve_icon_asset_strategy(source, committed_car=committed, sidecar=sidecar)
        return
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from pathlib import Path\n"
                "from scripts.build_engine_payload import PayloadBuildError\n"
                "from scripts.icon_composer_asset import resolve_icon_asset_strategy\n"
                "try:\n"
                "    resolve_icon_asset_strategy(\n"
                "        Path(sys.argv[1]),\n"
                "        committed_car=Path(sys.argv[2]),\n"
                "        sidecar=Path(sys.argv[3]),\n"
                "    )\n"
                "except PayloadBuildError as exc:\n"
                "    if 'stale or unprovenanced' in str(exc):\n"
                "        raise SystemExit(0)\n"
                "    raise\n"
                "raise SystemExit('expected stale provenance refusal')\n"
            ),
            str(source),
            str(committed),
            str(sidecar),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=cap,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.requirement("INSTALL-20")
def test_prepare_and_install_reuses_committed_catalog_without_actool(tmp_path: Path) -> None:
    """[if] actool is absent and the fallback matches [then] install copies it, [else stop]."""
    try:
        cap = require_no_actool_env()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    source = tmp_path / "AppIcon.icon"
    shutil.copytree(DEFAULT_ICON_SOURCE, source)
    committed = source / "Assets.car"
    sidecar = source / "Assets.car.source.sha256"
    if not committed.is_file():
        committed.write_bytes(b"committed-catalog")
    write_source_sha256_sidecar(
        sidecar,
        compute_source_digest(source),
        source=source,
    )
    app = tmp_path / "Open DJ.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    info_plist = app / "Contents" / "Info.plist"
    original = {"CFBundleIconFile": "icon.icns", "CFBundleIdentifier": "app.opendj.desktop"}
    with info_plist.open("wb") as fh:
        plistlib.dump(original, fh)
    if cap == "native":
        selected = prepare_and_install(app, source, committed_car=committed, sidecar=sidecar)
        assert selected == committed
    else:
        proc = run_icon_composer_cli(
            "--app",
            str(app),
            "--source",
            str(source),
            "--committed-car",
            str(committed),
            "--sidecar",
            str(sidecar),
            cwd=REPO_ROOT,
            no_actool=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "reused committed" in proc.stdout, proc.stdout
    assert (app / "Contents" / "Resources" / "Assets.car").read_bytes() == committed.read_bytes()
    with info_plist.open("rb") as fh:
        updated = plistlib.load(fh)
    assert updated["CFBundleIconName"] == ICON_SET_NAME
    for key, value in original.items():
        assert updated[key] == value


@pytest.mark.requirement("INSTALL-20")
def test_actool_available_strategy_requires_compile_not_fallback() -> None:
    """[if] actool is available [then] the strategy is compile despite a fallback, [else stop]."""
    if try_find_actool() is None:
        pytest.skip("UNAVAILABLE: actool is not on this host")
    if not DEFAULT_COMMITTED_ASSETS_CAR.is_file():
        pytest.skip("UNAVAILABLE: committed Assets.car fallback is not present")
    strategy = resolve_icon_asset_strategy()
    assert strategy.mode is IconAssetMode.COMPILE
    assert strategy.actool is not None


@pytest.mark.requirement("INSTALL-20")
def test_compile_failure_is_not_hidden_by_committed_catalog(tmp_path: Path) -> None:
    """[if] actool is invoked but fails [then] compile raises, no silent reuse, [else stop]."""
    fake_actool = tmp_path / "actool"
    fake_actool.write_text("#!/bin/sh\necho simulated actool failure >&2\nexit 42\n")
    fake_actool.chmod(0o755)
    with pytest.raises(PayloadBuildError, match="actool failed"):
        compile_icon_composer_asset(
            DEFAULT_ICON_SOURCE,
            actool=fake_actool,
            out_dir=tmp_path / "out",
        )


@pytest.mark.requirement("INSTALL-20")
def test_cli_check_accepts_explicit_paths(tmp_path: Path) -> None:
    """[if] --check is given explicit paths [then] it inspects that disposable tree, [else stop]."""
    try:
        require_no_actool_env()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    source = tmp_path / "AppIcon.icon"
    shutil.copytree(DEFAULT_ICON_SOURCE, source)
    committed = source / "Assets.car"
    sidecar = source / "Assets.car.source.sha256"
    committed.write_bytes(b"committed-catalog")
    write_source_sha256_sidecar(sidecar, compute_source_digest(source), source=source)
    proc = run_icon_composer_cli(
        "--check",
        "--source",
        str(source),
        "--committed-car",
        str(committed),
        "--sidecar",
        str(sidecar),
        cwd=REPO_ROOT,
        no_actool=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("reuse-committed path="), proc.stdout
