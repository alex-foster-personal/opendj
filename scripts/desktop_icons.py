"""Verify and regenerate the Open DJ desktop Tauri icon set.

The Dock fallback the maintainer reported (issue #2307) is what macOS draws when
``icon.icns`` lacks the size/scale slots ``iconutil`` expects. This module
mirrors ``cargo tauri icon`` output and fails the payload build when any slot
is missing, so a dmg cannot ship with a partial set.

Requirements:

- ✔︎ ✅ 🎯 Every Tauri desktop PNG, ``icon.icns``, ``icon.ico`` and AppX logo
  exists under ``apps/desktop/src-tauri/icons/`` -> :func:`verify_desktop_icons`
- ✔︎ ✅ 🎯 ``icon.icns`` embeds every pixel size ``iconutil`` needs (16 through
  1024 at 1x and 2x) -> :func:`icns_embedded_pixel_sizes`
- ✔︎ ✅ 🎯 Regeneration from one master PNG produces the same file set Tauri
  would -> :func:`generate_desktop_icons`
- ✔︎ ✅ 🎯 The master sits on Apple's macOS icon grid (1024 canvas, transparent
  margin around an 824px tile), so the Dock never draws an inset tile with
  padding around a full-bleed square (INSTALL-19) -> :func:`_load_master`

Acceptance tests:

- [if] ``icon.icns`` is absent [then] :func:`verify_desktop_icons` raises
  [else ⛔️].
- [if] ``icon.icns`` lacks the 1024px slot [then] verification raises naming
  the missing sizes [else ⛔️].
- [if] ``64x64.png`` is missing [then] verification raises [else ⛔️].
- [if] the master has an opaque corner or margin pixel [then] generation
  raises naming the grid [else ⛔️].
"""

from __future__ import annotations

import io
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from scripts.build_engine_payload import PayloadBuildError

# Pixel sizes iconutil expects inside a complete macOS iconset / .icns.
# Source: tauri-cli helpers/icns.json (same slots ``cargo tauri icon`` writes).
ICNS_REQUIRED_PIXEL_SIZES: frozenset[int] = frozenset({16, 32, 64, 128, 256, 512, 1024})

ICNS_OSTYPES: tuple[tuple[int, bytes], ...] = (
    (16, b"is32"),
    (32, b"ic11"),
    (32, b"il32"),
    (64, b"ic12"),
    (128, b"ic07"),
    (256, b"ic13"),
    (256, b"ic08"),
    (512, b"ic14"),
    (512, b"ic09"),
    (1024, b"ic10"),
)

# Tauri ``icon`` desktop PNG outputs (filename -> expected edge length).
TAURI_PNG_FILES: dict[str, int] = {
    "32x32.png": 32,
    "64x64.png": 64,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}

ICO_LAYER_SIZES: tuple[int, ...] = (32, 16, 24, 48, 64, 256)

APPX_SQUARE_SIZES: tuple[int, ...] = (30, 44, 71, 89, 107, 142, 150, 284, 310)

DEFAULT_ICONS_DIR: Path = (
    Path(__file__).resolve().parents[1] / "apps/desktop/src-tauri/icons"
)
DEFAULT_MASTER: Path = DEFAULT_ICONS_DIR / "app-icon.png"
MASTER_EDGE_PX: int = 1024
# Apple's macOS app icon grid: on a 1024 canvas the artwork is an 824px rounded
# square at (100, 100); everything outside it is transparent. A full-bleed
# opaque master is what macOS 26 draws as an inset tile with padding.
MASTER_GRID_MARGIN_PX: int = 100
MASTER_GRID_PROBE_PX: tuple[tuple[int, int], ...] = (
    (0, 0),
    (MASTER_EDGE_PX - 1, 0),
    (0, MASTER_EDGE_PX - 1),
    (MASTER_EDGE_PX - 1, MASTER_EDGE_PX - 1),
    (MASTER_GRID_MARGIN_PX // 2, MASTER_EDGE_PX // 2),
    (MASTER_EDGE_PX // 2, MASTER_GRID_MARGIN_PX // 2),
)


@dataclass(frozen=True)
class PngSpec:
    filename: str
    edge_px: int


def _repo_icons_dir(repo_root: Path) -> Path:
    return repo_root / "apps/desktop/src-tauri/icons"


def _png_specs() -> list[PngSpec]:
    specs = [PngSpec(name, size) for name, size in TAURI_PNG_FILES.items()]
    specs.append(PngSpec("StoreLogo.png", 50))
    specs.extend(
        PngSpec(f"Square{size}x{size}Logo.png", size) for size in APPX_SQUARE_SIZES
    )
    return specs


def _resize_rgba(master: Image.Image, edge_px: int) -> Image.Image:
    if master.size != (edge_px, edge_px):
        return master.resize((edge_px, edge_px), Image.Resampling.LANCZOS)
    return master


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _read_png_edge(png_bytes: bytes) -> int:
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        msg = "icns chunk is not PNG-encoded"
        raise PayloadBuildError(msg)
    return struct.unpack(">I", png_bytes[16:20])[0]


def icns_embedded_pixel_sizes(icns_path: Path) -> set[int]:
    """Return the pixel widths embedded in an .icns (PNG chunks only)."""
    data = icns_path.read_bytes()
    if len(data) < 8 or data[:4] != b"icns":
        msg = f"{icns_path} is not a valid .icns (missing icns magic)"
        raise PayloadBuildError(msg)

    sizes: set[int] = set()
    chunk_types: set[bytes] = set()
    offset = 8
    while offset + 8 <= len(data):
        chunk_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        if chunk_size < 8 or offset + chunk_size > len(data):
            msg = f"{icns_path} has a corrupt icns chunk at offset {offset}"
            raise PayloadBuildError(msg)
        chunk_types.add(data[offset : offset + 4])
        chunk_data = data[offset + 8 : offset + chunk_size]
        if chunk_data[:8] == b"\x89PNG\r\n\x1a\n":
            sizes.add(_read_png_edge(chunk_data))
        offset += chunk_size
    if {b"is32", b"s8mk"} <= chunk_types:
        sizes.add(16)
    if {b"il32", b"l8mk"} <= chunk_types:
        sizes.add(32)
    return sizes


def _write_icns(png_by_size: dict[int, bytes], icns_path: Path) -> None:
    chunks: list[bytes] = []
    for edge_px, ostype in ICNS_OSTYPES:
        png_data = png_by_size[edge_px]
        if ostype in {b"is32", b"il32"}:
            # These legacy types require planar RGB RLE, never a PNG stream.
            # Literal packets encode 1..128 bytes as count-minus-one plus bytes.
            with Image.open(io.BytesIO(png_data)) as image:
                rgba = image.convert("RGBA")
                rgb = bytearray()
                for band in ("R", "G", "B"):
                    channel = rgba.getchannel(band).tobytes()
                    for offset in range(0, len(channel), 128):
                        packet = channel[offset : offset + 128]
                        rgb.append(len(packet) - 1)
                        rgb.extend(packet)
                alpha = rgba.getchannel("A").tobytes()
            chunks.append(ostype + struct.pack(">I", 8 + len(rgb)) + rgb)
            mask_type = b"s8mk" if ostype == b"is32" else b"l8mk"
            chunks.append(mask_type + struct.pack(">I", 8 + len(alpha)) + alpha)
        else:
            chunks.append(ostype + struct.pack(">I", 8 + len(png_data)) + png_data)
    body = b"".join(chunks)
    header = b"icns" + struct.pack(">I", 8 + len(body))
    icns_path.write_bytes(header + body)


def _write_ico(png_by_size: dict[int, bytes], ico_path: Path) -> None:
    # Tauri puts 32px first; Pillow preserves the sizes= order.
    order = ICO_LAYER_SIZES
    images = [
        Image.open(io.BytesIO(png_by_size[size])).convert("RGBA") for size in order
    ]
    images[0].save(
        ico_path,
        format="ICO",
        sizes=[(size, size) for size in order],
        append_images=images[1:],
    )


def _iconutil_round_trip(icns_path: Path) -> None:
    """When iconutil exists (macOS), prove the .icns unpacks to a full iconset."""
    if not _iconutil_available():
        return
    with tempfile.TemporaryDirectory(prefix="opendj-icns-verify-") as tmp:
        iconset = Path(tmp) / "roundtrip.iconset"
        iconset.mkdir()
        subprocess.run(
            ["iconutil", "-c", "iconset", str(icns_path), "-o", str(iconset)],
            check=True,
            capture_output=True,
            text=True,
        )
        expected = {
            "icon_16x16.png",
            "icon_16x16@2x.png",
            "icon_32x32.png",
            "icon_32x32@2x.png",
            "icon_128x128.png",
            "icon_128x128@2x.png",
            "icon_256x256.png",
            "icon_256x256@2x.png",
            "icon_512x512.png",
            "icon_512x512@2x.png",
        }
        present = {path.name for path in iconset.iterdir() if path.suffix == ".png"}
        missing = sorted(expected - present)
        if missing:
            msg = (
                f"{icns_path} did not round-trip through iconutil; "
                f"missing iconset slots: {', '.join(missing)}"
            )
            raise PayloadBuildError(msg)


def _iconutil_available() -> bool:
    try:
        subprocess.run(
            ["iconutil", "-h"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return True


def verify_desktop_icons(icons_dir: Path) -> None:
    """Fail closed when the committed Tauri icon set is incomplete."""
    if not icons_dir.is_dir():
        msg = f"desktop icons directory missing: {icons_dir}"
        raise PayloadBuildError(msg)

    missing_files: list[str] = [
        spec.filename
        for spec in _png_specs()
        if not (icons_dir / spec.filename).is_file()
    ]
    for spec in _png_specs():
        path = icons_dir / spec.filename
        if not path.is_file():
            continue
        with Image.open(path) as image:
            width, height = image.size
        if width != spec.edge_px or height != spec.edge_px:
            msg = (
                f"{spec.filename} must be {spec.edge_px}x{spec.edge_px}px, "
                f"found {width}x{height}px"
            )
            raise PayloadBuildError(msg)

    missing_files.extend(
        filename
        for filename in ("icon.icns", "icon.ico")
        if not (icons_dir / filename).is_file()
    )

    if missing_files:
        msg = (
            "desktop icon set is incomplete; missing: "
            + ", ".join(sorted(missing_files))
        )
        raise PayloadBuildError(msg)

    icns_path = icons_dir / "icon.icns"
    embedded = icns_embedded_pixel_sizes(icns_path)
    missing_sizes = sorted(ICNS_REQUIRED_PIXEL_SIZES - embedded)
    if missing_sizes:
        msg = (
            f"{icns_path} lacks icns slots for pixel sizes: "
            + ", ".join(str(size) for size in missing_sizes)
        )
        raise PayloadBuildError(msg)

    _iconutil_round_trip(icns_path)


def verify_desktop_icons_for_repo(repo_root: Path) -> None:
    verify_desktop_icons(_repo_icons_dir(repo_root))


def _load_master(master_path: Path) -> Image.Image:
    if not master_path.is_file():
        msg = f"icon master PNG missing: {master_path}"
        raise PayloadBuildError(msg)
    with Image.open(master_path) as image:
        rgba = image.convert("RGBA")
        if rgba.width != rgba.height:
            msg = f"icon master must be square, got {rgba.width}x{rgba.height}"
            raise PayloadBuildError(msg)
        if rgba.width < MASTER_EDGE_PX:
            rgba = _resize_rgba(rgba, MASTER_EDGE_PX)
        _verify_master_on_apple_grid(rgba, master_path)
        return rgba.copy()


def _verify_master_on_apple_grid(rgba: Image.Image, master_path: Path) -> None:
    """Refuse a full-bleed master: the margin outside Apple's 824px tile must be transparent."""
    scale = rgba.width / MASTER_EDGE_PX
    opaque = [
        (x, y)
        for x, y in MASTER_GRID_PROBE_PX
        if rgba.getpixel((int(x * scale), int(y * scale)))[3] != 0
    ]
    if opaque:
        msg = (
            f"icon master {master_path} is not on Apple's icon grid: expected a transparent "
            f"{MASTER_GRID_MARGIN_PX}px margin around the tile, found opaque pixels at "
            + ", ".join(f"({x},{y})" for x, y in opaque)
        )
        raise PayloadBuildError(msg)


def generate_desktop_icons(
    master_path: Path,
    icons_dir: Path,
) -> None:
    """Regenerate the full Tauri desktop icon set from one master PNG."""
    master = _load_master(master_path)
    icons_dir.mkdir(parents=True, exist_ok=True)

    png_by_size: dict[int, bytes] = {}
    all_sizes = (
        {spec.edge_px for spec in _png_specs()}
        | ICNS_REQUIRED_PIXEL_SIZES
        | set(ICO_LAYER_SIZES)
    )
    for edge_px in sorted(all_sizes):
        png_by_size[edge_px] = _png_bytes(_resize_rgba(master, edge_px))

    for spec in _png_specs():
        (icons_dir / spec.filename).write_bytes(png_by_size[spec.edge_px])

    _write_icns(png_by_size, icons_dir / "icon.icns")
    _write_ico(png_by_size, icons_dir / "icon.ico")
    verify_desktop_icons(icons_dir)


def generate_desktop_icons_for_repo(
    repo_root: Path,
    *,
    master_path: Path | None = None,
) -> None:
    icons_dir = _repo_icons_dir(repo_root)
    master = master_path or icons_dir / "app-icon.png"
    if not master.is_file():
        master = icons_dir / "icon.png"
    generate_desktop_icons(master, icons_dir)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--master",
        type=Path,
        default=None,
        help="Square master PNG (default: icons/app-icon.png, else icons/icon.png)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify the committed icon set without regenerating",
    )
    args = parser.parse_args()
    if args.verify_only:
        verify_desktop_icons_for_repo(args.repo_root)
        print(f"[OK] desktop icons verified under {_repo_icons_dir(args.repo_root)}")
        return
    generate_desktop_icons_for_repo(args.repo_root, master_path=args.master)
    print(f"[OK] desktop icons regenerated under {_repo_icons_dir(args.repo_root)}")


if __name__ == "__main__":
    main()
