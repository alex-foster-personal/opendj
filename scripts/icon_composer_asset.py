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

When ``actool`` is absent, ``just dmg`` may reuse a committed ``Assets.car``
only when a sidecar records the exact SHA-256 of the current Icon Composer
source tree. A changed source without ``actool`` is a hard failure, never a
silent stale icon (issue #2633).

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
- ✔︎ A verified committed ``Assets.car`` may be reused when the source digest
  matches -> :func:`resolve_icon_asset_strategy`

Acceptance tests:

- [if] ``icon.json`` fill does not match the app-icon.png tile color [then]
  verification raises [else ⛔️].
- [if] the layer PNG is fully transparent [then] verification raises [else ⛔️].
- [if] ``actool`` is not on this host (command line tools only) [then]
  compiling raises naming Xcode as the remedy [else ⛔️].
- [if] installing into a bundle whose Info.plist lacks CFBundleIconName
  [then] the key is added and every other key is unchanged [else ⛔️].
- [if] actool is absent but the committed catalog matches the source digest
  [then] the committed path is selected [else ⛔️].
- [if] actool is absent and the source digest differs from the sidecar [then]
  it raises naming stale provenance [else ⛔️].
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image

from scripts.build_engine_payload import PayloadBuildError

ICON_SET_NAME: str = "AppIcon"
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_ICON_SOURCE: Path = REPO_ROOT / "apps/desktop/src-tauri/icons/AppIcon.icon"
ASSETS_CAR_FILENAME: str = "Assets.car"
SOURCE_SHA256_SIDECAR_FILENAME: str = "Assets.car.source.sha256"
DEFAULT_COMMITTED_ASSETS_CAR: Path = DEFAULT_ICON_SOURCE / ASSETS_CAR_FILENAME
DEFAULT_SOURCE_SHA256_SIDECAR: Path = DEFAULT_ICON_SOURCE / SOURCE_SHA256_SIDECAR_FILENAME
SOURCE_SHA256_PREFIX: str = "source-sha256="
# Must match app-icon.png's tile fill (INSTALL-19) so the flat area behind the
# mark's transparent margin is the same dark as the legacy master.
EXPECTED_FILL_RGB: tuple[int, int, int] = (0x0D, 0x0F, 0x12)
FILL_TOLERANCE: float = 0.002  # extended-srgb rounds to 5 decimal places


class IconAssetMode(str, Enum):
    COMPILE = "compile"
    REUSE_COMMITTED = "reuse-committed"


@dataclass(frozen=True)
class IconAssetStrategy:
    mode: IconAssetMode
    source_digest: str
    actool: Path | None = None
    committed_car: Path | None = None


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


# ----- source digest and committed fallback --------------------------------


def _host_label() -> str:
    return socket.gethostname()


def _generated_artifact_names() -> set[str]:
    return {ASSETS_CAR_FILENAME, SOURCE_SHA256_SIDECAR_FILENAME}


def compute_source_digest(source: Path = DEFAULT_ICON_SOURCE) -> str:
    """Deterministic SHA-256 over sorted Icon Composer inputs, excluding generated files."""
    verify_icon_composer_source(source)
    hasher = hashlib.sha256()
    files = sorted(
        (
            path
            for path in source.rglob("*")
            if path.is_file() and path.name not in _generated_artifact_names()
        ),
        key=lambda path: path.relative_to(source).as_posix(),
    )
    for path in files:
        rel = path.relative_to(source).as_posix()
        hasher.update(rel.encode())
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def read_source_sha256_sidecar(sidecar: Path = DEFAULT_SOURCE_SHA256_SIDECAR) -> str:
    if not sidecar.is_file():
        msg = f"Icon Composer provenance sidecar missing: {sidecar}"
        raise PayloadBuildError(msg)
    for line in sidecar.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(SOURCE_SHA256_PREFIX):
            digest = stripped[len(SOURCE_SHA256_PREFIX) :].strip()
            if digest:
                return digest
    msg = (
        f"{sidecar} does not contain a {SOURCE_SHA256_PREFIX}<digest> line "
        "identifying the Icon Composer source digest"
    )
    raise PayloadBuildError(msg)


def write_source_sha256_sidecar(
    sidecar: Path,
    digest: str,
    *,
    source: Path = DEFAULT_ICON_SOURCE,
) -> None:
    sidecar.write_text(
        f"{SOURCE_SHA256_PREFIX}{digest}\n"
        f"# digest covers every file under {source.name}/ except "
        f"{ASSETS_CAR_FILENAME} and {SOURCE_SHA256_SIDECAR_FILENAME}\n"
    )


def _actool_unavailable_message(host: str) -> str:
    return (
        f"actool not found on {host}; Icon Composer compilation needs a full "
        "Xcode 26+ install (command line tools alone do not ship actool). Run "
        "'xcode-select -s /Applications/Xcode.app' on a host that has it, "
        "or set DEVELOPER_DIR."
    )


def _stale_committed_catalog_message(
    host: str,
    *,
    source: Path,
    committed_car: Path,
    sidecar: Path,
    current_digest: str,
    recorded_digest: str | None = None,
) -> str:
    recorded = recorded_digest or "(missing or unreadable)"
    return (
        f"actool not found on {host} and the committed Icon Composer catalog "
        f"at {committed_car} is stale or unprovenanced for {source}. "
        f"Current source digest is {current_digest}; sidecar {sidecar} records "
        f"{recorded}. Regenerate {ASSETS_CAR_FILENAME} and "
        f"{SOURCE_SHA256_SIDECAR_FILENAME} on a host with full Xcode, or install "
        "Xcode and re-run so actool can compile the changed source."
    )


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
    host = _host_label()
    try:
        result = subprocess.run(
            ["xcrun", "--find", "actool"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        msg = (
            f"actool not found on {host}; Icon Composer compilation needs a full "
            "Xcode 26+ install (command line tools alone do not ship actool, and "
            f"this host has no xcrun at all: {exc}). Run "
            "'xcode-select -s /Applications/Xcode.app' on a host that has it, "
            "or set DEVELOPER_DIR."
        )
        raise PayloadBuildError(msg) from exc
    if result.returncode != 0 or not result.stdout.strip():
        msg = _actool_unavailable_message(host)
        raise PayloadBuildError(msg)
    return Path(result.stdout.strip())


def try_find_actool() -> Path | None:
    try:
        return find_actool()
    except PayloadBuildError:
        return None


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
    assets_car = work_dir / ASSETS_CAR_FILENAME
    if result.returncode != 0 or not assets_car.is_file() or assets_car.stat().st_size == 0:
        msg = (
            f"actool failed to compile {source} into {ASSETS_CAR_FILENAME} "
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


# ----- strategy selection ---------------------------------------------------


def resolve_icon_asset_strategy(
    source: Path = DEFAULT_ICON_SOURCE,
    *,
    committed_car: Path = DEFAULT_COMMITTED_ASSETS_CAR,
    sidecar: Path = DEFAULT_SOURCE_SHA256_SIDECAR,
) -> IconAssetStrategy:
    """Choose compile vs committed reuse; shared by preflight and install."""
    source_digest = compute_source_digest(source)
    actool_bin = try_find_actool()
    if actool_bin is not None:
        return IconAssetStrategy(
            mode=IconAssetMode.COMPILE,
            source_digest=source_digest,
            actool=actool_bin,
        )

    host = _host_label()
    if not committed_car.is_file() or committed_car.stat().st_size == 0:
        msg = _stale_committed_catalog_message(
            host,
            source=source,
            committed_car=committed_car,
            sidecar=sidecar,
            current_digest=source_digest,
        )
        raise PayloadBuildError(msg)

    try:
        recorded_digest = read_source_sha256_sidecar(sidecar)
    except PayloadBuildError:
        msg = _stale_committed_catalog_message(
            host,
            source=source,
            committed_car=committed_car,
            sidecar=sidecar,
            current_digest=source_digest,
        )
        raise PayloadBuildError(msg) from None

    if recorded_digest != source_digest:
        msg = _stale_committed_catalog_message(
            host,
            source=source,
            committed_car=committed_car,
            sidecar=sidecar,
            current_digest=source_digest,
            recorded_digest=recorded_digest,
        )
        raise PayloadBuildError(msg)

    return IconAssetStrategy(
        mode=IconAssetMode.REUSE_COMMITTED,
        source_digest=source_digest,
        committed_car=committed_car,
    )


def check_icon_composer_asset(
    source: Path = DEFAULT_ICON_SOURCE,
    *,
    committed_car: Path = DEFAULT_COMMITTED_ASSETS_CAR,
    sidecar: Path = DEFAULT_SOURCE_SHA256_SIDECAR,
) -> str:
    """Read-only preflight probe; returns a one-line status for shell callers."""
    strategy = resolve_icon_asset_strategy(source, committed_car=committed_car, sidecar=sidecar)
    if strategy.mode is IconAssetMode.COMPILE:
        assert strategy.actool is not None
        return (
            f"compile actool={strategy.actool} source-sha256={strategy.source_digest}"
        )
    assert strategy.committed_car is not None
    return (
        f"reuse-committed path={strategy.committed_car} "
        f"source-sha256={strategy.source_digest}"
    )


# ----- install into a built .app -----------------------------------------------


def install_into_app_bundle(app_path: Path, assets_car: Path) -> None:
    """Copy Assets.car into Contents/Resources and set CFBundleIconName."""
    resources = app_path / "Contents" / "Resources"
    if not resources.is_dir():
        msg = f"not a built .app bundle (missing Resources): {app_path}"
        raise PayloadBuildError(msg)

    shutil.copy2(assets_car, resources / ASSETS_CAR_FILENAME)

    info_plist_path = app_path / "Contents" / "Info.plist"
    with info_plist_path.open("rb") as fh:
        info = plistlib.load(fh)
    info["CFBundleIconName"] = ICON_SET_NAME
    with info_plist_path.open("wb") as fh:
        plistlib.dump(info, fh)


def prepare_and_install(
    app_path: Path,
    source: Path = DEFAULT_ICON_SOURCE,
    *,
    committed_car: Path = DEFAULT_COMMITTED_ASSETS_CAR,
    sidecar: Path = DEFAULT_SOURCE_SHA256_SIDECAR,
) -> Path:
    """Compile or reuse the committed catalog, then install into app_path."""
    strategy = resolve_icon_asset_strategy(source, committed_car=committed_car, sidecar=sidecar)
    if strategy.mode is IconAssetMode.COMPILE:
        assets_car = compile_icon_composer_asset(source, actool=strategy.actool)
        print(
            f"[OK] Icon Composer compiled {assets_car} "
            f"(source-sha256={strategy.source_digest}) -> {app_path}/Contents/Resources"
        )
    else:
        assert strategy.committed_car is not None
        assets_car = strategy.committed_car
        print(
            f"[OK] Icon Composer reused committed {assets_car} "
            f"(source-sha256={strategy.source_digest}) -> {app_path}/Contents/Resources"
        )
    install_into_app_bundle(app_path, assets_car)
    print(f"[OK] CFBundleIconName={ICON_SET_NAME}")
    return assets_car


def compile_and_install(app_path: Path, source: Path = DEFAULT_ICON_SOURCE) -> Path:
    """Compatibility wrapper around :func:`prepare_and_install`."""
    return prepare_and_install(app_path, source)


# ----- CLI -----------------------------------------------


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="path to the built .app bundle")
    parser.add_argument("--check", action="store_true", help="read-only preflight probe")
    parser.add_argument("--source", type=Path, default=DEFAULT_ICON_SOURCE)
    parser.add_argument(
        "--committed-car",
        type=Path,
        default=None,
        help=f"committed {ASSETS_CAR_FILENAME} path (default: beside --source)",
    )
    parser.add_argument(
        "--sidecar",
        type=Path,
        default=None,
        help=f"source digest sidecar path (default: beside --source)",
    )
    args = parser.parse_args()

    committed_car = args.committed_car or (args.source / ASSETS_CAR_FILENAME)
    sidecar = args.sidecar or (args.source / SOURCE_SHA256_SIDECAR_FILENAME)

    if args.check:
        print(
            check_icon_composer_asset(
                args.source,
                committed_car=committed_car,
                sidecar=sidecar,
            )
        )
        return

    if args.app is None:
        parser.error("--app is required unless --check is set")

    prepare_and_install(
        args.app,
        args.source,
        committed_car=committed_car,
        sidecar=sidecar,
    )


if __name__ == "__main__":
    try:
        _main()
    except PayloadBuildError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
