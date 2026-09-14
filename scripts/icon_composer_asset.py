"""Compile and install the macOS 26 Icon Composer asset (INSTALL-20, issue #2566).

macOS 26 (Tahoe) auto-normalizes a legacy ``icon.icns`` that ships without a
matching Icon Composer catalog: even a master that fills Apple's icon grid
(INSTALL-19) can still be composited onto the system's own gray Dock plate,
because the OS has no compiled asset to draw edge-to-edge with its own Liquid
Glass squircle and specular highlight. The fix Apple ships for this is a
layered ``.icon`` document compiled with ``actool`` into ``Assets.car`` and
referenced from ``CFBundleIconName``; the legacy ``icon.icns`` stays as the
``CFBundleIconFile`` fallback for macOS 15 and earlier.

``actool`` requires a full Xcode install (confirmed empirically on this
machine and silver: command line tools alone refuse it), so this module
fails fast with a named remedy rather than silently skipping the step -- a
dmg built without the compiled catalog on a host that CAN produce one is the
exact bug this exists to catch.

Requirements:

- ✔︎ ✅ 🎯 The committed ``.icon`` source is valid: JSON parses, the fill
  matches the app-icon.png tile color, and its one layer PNG exists and is
  not fully transparent -> :func:`verify_icon_composer_source`
- ✔︎ ✅ Compiling the source with ``actool`` produces a non-empty
  ``Assets.car`` naming the ``AppIcon`` icon stack -> :func:`compile_icon_composer_asset`
- ✔︎ ✅ Installing into a built ``.app`` copies ``Assets.car`` into
  ``Contents/Resources`` and sets ``CFBundleIconName`` without disturbing any
  other Info.plist key -> :func:`install_into_app_bundle`
- ✔︎ Missing ``actool`` (command line tools only) fails with a message naming
  the remedy, never a silent skip -> :func:`find_actool`

Acceptance tests:

- [if] ``icon.json`` fill does not match the app-icon.png tile color [then]
  verification raises [else ⛔️].
- [if] the layer PNG is fully transparent [then] verification raises [else ⛔️].
- [if] ``actool`` is not on this host (command line tools only) [then]
  compiling raises naming Xcode as the remedy [else ⛔️].
- [if] installing into a bundle whose Info.plist lacks CFBundleIconName
  [then] the key is added and every other key is unchanged [else ⛔️].
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from scripts.build_engine_payload import PayloadBuildError

ICON_SET_NAME: str = "AppIcon"
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_ICON_SOURCE: Path = REPO_ROOT / "apps/desktop/src-tauri/icons/AppIcon.icon"
# Must match app-icon.png's tile fill (INSTALL-19) so the flat area behind the
# mark's transparent margin is the same dark as the legacy master.
EXPECTED_FILL_RGB: tuple[int, int, int] = (0x0D, 0x0F, 0x12)
FILL_TOLERANCE: float = 0.002  # extended-srgb rounds to 5 decimal places


# ----- source validation -----------------------------------------------


def _fill_matches_expected(fill_solid: str) -> bool:
    prefix = "extended-srgb:"
    if not fill_solid.startswith(prefix):
        return False
    parts = fill_solid[len(prefix) :].split(",")
    if len(parts) != 4:
        return False
    r, g, b, _a = (float(p) for p in parts)
    expected = [c / 255 for c in EXPECTED_FILL_RGB]
    return all(
        abs(actual - want) <= FILL_TOLERANCE
        for actual, want in zip((r, g, b), expected, strict=True)
    )


def verify_icon_composer_source(source: Path = DEFAULT_ICON_SOURCE) -> None:
    """Fail closed when the committed .icon document cannot produce a sane asset."""
    icon_json = source / "icon.json"
    if not icon_json.is_file():
        msg = f"Icon Composer source missing icon.json: {icon_json}"
        raise PayloadBuildError(msg)

    spec = json.loads(icon_json.read_text())
    fill_solid = spec.get("fill", {}).get("solid", "")
    if not _fill_matches_expected(fill_solid):
        msg = (
            f"{icon_json} fill {fill_solid!r} does not match the app-icon.png "
            f"tile color {EXPECTED_FILL_RGB}"
        )
        raise PayloadBuildError(msg)

    groups = spec.get("groups", [])
    if not groups or not groups[0].get("layers"):
        msg = f"{icon_json} has no groups/layers to compile"
        raise PayloadBuildError(msg)

    image_name = groups[0]["layers"][0]["image-name"]
    layer_path = source / "Assets" / image_name
    if not layer_path.is_file():
        msg = f"{icon_json} references missing layer asset: {layer_path}"
        raise PayloadBuildError(msg)

    with Image.open(layer_path) as image:
        rgba = image.convert("RGBA")
        if rgba.getchannel("A").getbbox() is None:
            msg = f"{layer_path} is fully transparent; the mark would not render"
            raise PayloadBuildError(msg)


# ----- actool compile -----------------------------------------------


def find_actool() -> Path:
    """Locate actool, refusing a command-line-tools-only developer directory.

    actool ships only with full Xcode (verified empirically: xcrun refuses it
    under /Library/Developer/CommandLineTools), so a missing actool here
    means "select Xcode 26+", not "skip this step". This also covers a host
    with no ``xcrun`` at all (e.g. a Linux CI runner): a missing binary is
    the same "cannot compile here" fact as a resolvable-but-empty xcrun, so
    both raise the same named-remedy error instead of one of them crashing
    with an unhandled FileNotFoundError.
    """
    try:
        result = subprocess.run(
            ["xcrun", "--find", "actool"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        msg = (
            "actool not found; Icon Composer compilation needs a full Xcode 26+ "
            "install (command line tools alone do not ship actool, and this host "
            f"has no xcrun at all: {exc}). Run "
            "'xcode-select -s /Applications/Xcode.app' on a host that has it, "
            "or set DEVELOPER_DIR."
        )
        raise PayloadBuildError(msg) from exc
    if result.returncode != 0 or not result.stdout.strip():
        msg = (
            "actool not found; Icon Composer compilation needs a full Xcode 26+ "
            "install (command line tools alone do not ship actool). Run "
            "'xcode-select -s /Applications/Xcode.app' on a host that has it, "
            "or set DEVELOPER_DIR."
        )
        raise PayloadBuildError(msg)
    return Path(result.stdout.strip())


def compile_icon_composer_asset(
    source: Path = DEFAULT_ICON_SOURCE,
    *,
    actool: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Compile the .icon source into Assets.car; return its path."""
    verify_icon_composer_source(source)
    actool_bin = actool or find_actool()
    work_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="opendj-icon-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            str(actool_bin),
            "--compile",
            str(work_dir),
            "--platform",
            "macosx",
            "--minimum-deployment-target",
            "15.0",
            "--app-icon",
            ICON_SET_NAME,
            "--output-partial-info-plist",
            str(work_dir / "asset-info.plist"),
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assets_car = work_dir / "Assets.car"
    if result.returncode != 0 or not assets_car.is_file() or assets_car.stat().st_size == 0:
        msg = (
            f"actool failed to compile {source} into Assets.car "
            f"(exit {result.returncode}): {result.stdout}\n{result.stderr}"
        )
        raise PayloadBuildError(msg)

    verify_result = subprocess.run(
        ["/usr/bin/assetutil", "--info", str(assets_car)],
        check=False,
        capture_output=True,
        text=True,
    )
    if verify_result.returncode != 0 or ICON_SET_NAME not in verify_result.stdout:
        msg = f"compiled {assets_car} does not name the {ICON_SET_NAME} icon stack"
        raise PayloadBuildError(msg)

    return assets_car


# ----- install into a built .app -----------------------------------------------


def install_into_app_bundle(app_path: Path, assets_car: Path) -> None:
    """Copy Assets.car into Contents/Resources and set CFBundleIconName."""
    resources = app_path / "Contents" / "Resources"
    if not resources.is_dir():
        msg = f"not a built .app bundle (missing Resources): {app_path}"
        raise PayloadBuildError(msg)

    shutil.copy2(assets_car, resources / "Assets.car")

    info_plist_path = app_path / "Contents" / "Info.plist"
    with info_plist_path.open("rb") as fh:
        info = plistlib.load(fh)
    info["CFBundleIconName"] = ICON_SET_NAME
    with info_plist_path.open("wb") as fh:
        plistlib.dump(info, fh)


def compile_and_install(app_path: Path, source: Path = DEFAULT_ICON_SOURCE) -> Path:
    """End-to-end: compile the committed source and install it into app_path."""
    assets_car = compile_icon_composer_asset(source)
    install_into_app_bundle(app_path, assets_car)
    return assets_car


# ----- CLI -----------------------------------------------


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="path to the built .app bundle")
    parser.add_argument("--source", type=Path, default=DEFAULT_ICON_SOURCE)
    args = parser.parse_args()
    assets_car = compile_and_install(args.app, args.source)
    print(f"[OK] installed {assets_car} -> {args.app}/Resources, CFBundleIconName={ICON_SET_NAME}")


if __name__ == "__main__":
    _main()
