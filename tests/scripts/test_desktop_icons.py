"""Desktop Tauri icon set verification (issue #2307)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from scripts.build_engine_payload import PayloadBuildError
from scripts.desktop_icons import (
    ICNS_REQUIRED_PIXEL_SIZES,
    MASTER_GRID_PROBE_PX,
    generate_desktop_icons,
    icns_embedded_pixel_sizes,
    verify_desktop_icons,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ICONS_DIR: Path = REPO_ROOT / "apps/desktop/src-tauri/icons"


def _grid_master(path: Path, fill: tuple[int, int, int, int]) -> Path:
    """A master on Apple's grid: transparent canvas, 824px rounded tile at (100, 100)."""
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle((100, 100, 923, 923), radius=185, fill=fill)
    image.save(path)
    return path


# REQ: INSTALL-17
@pytest.mark.requirement("INSTALL-17")
def test_committed_icon_set_is_complete() -> None:
    """[if] any Tauri slot is missing [then] payload verify must refuse the build, [else stop]."""
    verify_desktop_icons(ICONS_DIR)


# REQ: INSTALL-17
@pytest.mark.requirement("INSTALL-17")
def test_incomplete_icon_set_is_refused(tmp_path: Path) -> None:
    """[if] verify runs on an empty icon directory [then] it raises incomplete, [else stop]."""
    icons = tmp_path / "icons"
    icons.mkdir()
    with pytest.raises(PayloadBuildError, match="incomplete"):
        verify_desktop_icons(icons)


# REQ: INSTALL-17
@pytest.mark.requirement("INSTALL-17")
def test_icns_missing_1024_slot_is_refused(tmp_path: Path) -> None:
    """[if] icon.icns lacks the 1024px slot [then] verify names 1024, [else stop]."""
    master = _grid_master(tmp_path / "master.png", (200, 100, 50, 255))
    icons = tmp_path / "icons"
    generate_desktop_icons(master, icons)

    icns_path = icons / "icon.icns"
    data = bytearray(icns_path.read_bytes())
    # Drop the 1024px ic10 chunk so verification catches a partial .icns.
    offset = 8
    filtered = bytearray(data[:8])
    while offset + 8 <= len(data):
        chunk_type = data[offset : offset + 4]
        chunk_size = int.from_bytes(data[offset + 4 : offset + 8], "big")
        chunk = data[offset : offset + chunk_size]
        if chunk_type != b"ic10":
            filtered.extend(chunk)
        offset += chunk_size
    filtered[4:8] = len(filtered).to_bytes(4, "big")
    icns_path.write_bytes(filtered)

    embedded = icns_embedded_pixel_sizes(icns_path)
    assert 1024 not in embedded
    with pytest.raises(PayloadBuildError, match="1024"):
        verify_desktop_icons(icons)


@pytest.mark.requirement("INSTALL-17")
def test_generation_embeds_every_iconutil_pixel_size(tmp_path: Path) -> None:
    """[if] generation omits an iconutil pixel size [then] embedded sizes diverge, [else stop]."""
    master = _grid_master(tmp_path / "master.png", (217, 119, 87, 255))
    icons = tmp_path / "icons"
    generate_desktop_icons(master, icons)
    embedded = icns_embedded_pixel_sizes(icons / "icon.icns")
    assert embedded == ICNS_REQUIRED_PIXEL_SIZES


@pytest.mark.requirement("INSTALL-19")
def test_full_bleed_master_is_refused(tmp_path: Path) -> None:
    """[if] the master is an opaque full-bleed square [then] generation refuses it, [else stop]."""
    master = tmp_path / "master.png"
    Image.new("RGBA", (1024, 1024), (13, 15, 18, 255)).save(master)
    with pytest.raises(PayloadBuildError, match="icon grid"):
        generate_desktop_icons(master, tmp_path / "icons")


@pytest.mark.requirement("INSTALL-19")
def test_committed_master_sits_on_apple_grid() -> None:
    """[if] a committed-master margin probe is opaque [then] the Dock insets it, [else stop]."""
    with Image.open(ICONS_DIR / "app-icon.png") as image:
        rgba = image.convert("RGBA")
        assert rgba.size == (1024, 1024)
        alphas = [rgba.getpixel(p)[3] for p in MASTER_GRID_PROBE_PX]
        assert alphas == [0] * len(MASTER_GRID_PROBE_PX)
        # and the tile itself is opaque, so the check is not passing on an empty image
        assert rgba.getpixel((512, 120))[3] == 255

ICONSET_SLOTS = {
    "icon_16x16.png": (16, 1), "icon_16x16@2x.png": (16, 2),
    "icon_32x32.png": (32, 1), "icon_32x32@2x.png": (32, 2),
    "icon_128x128.png": (128, 1), "icon_128x128@2x.png": (128, 2),
    "icon_256x256.png": (256, 1), "icon_256x256@2x.png": (256, 2),
    "icon_512x512.png": (512, 1), "icon_512x512@2x.png": (512, 2),
}


def _assert_master_pixels(image: Image.Image, edge: int) -> None:
    with Image.open(ICONS_DIR / "app-icon.png") as master:
        expected = master.convert("RGBA").resize((edge, edge), Image.Resampling.LANCZOS)
    actual = image.convert("RGBA")
    assert actual.size == expected.size
    assert actual.tobytes() == expected.tobytes()


@pytest.mark.requirement("INSTALL-17")
def test_committed_icns_all_frames_preserve_real_master_pixels() -> None:
    """[if] a committed ICNS frame misdecodes [then] real master pixels differ, [else stop]."""
    with Image.open(ICONS_DIR / "icon.icns") as image:
        sizes = {(edge, edge, scale) for edge, scale in ICONSET_SLOTS.values()}
        assert set(image.info["sizes"]) == sizes
        for edge, scale in ICONSET_SLOTS.values():
            _assert_master_pixels(image.icns.getimage((edge, edge, scale)), edge * scale)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Darwin native iconutil ICNS decoder is unavailable off macOS",
)
@pytest.mark.requirement("INSTALL-17")
def test_generated_icns_native_decode_preserves_real_master_pixels(tmp_path: Path) -> None:
    """[if] native ICNS decoding corrupts a slot [then] real master pixels differ, [else stop]."""
    iconutil = shutil.which("iconutil")
    assert iconutil is not None, "UNAVAILABLE: Darwin native iconutil decoder"
    master = ICONS_DIR / "app-icon.png"
    before = hashlib.sha256(master.read_bytes()).hexdigest()
    generated = tmp_path / "generated"
    generate_desktop_icons(master, generated)
    unpacked = tmp_path / "native.iconset"
    subprocess.run(
        [iconutil, "-c", "iconset", str(generated / "icon.icns"), "-o", str(unpacked)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert {p.name for p in unpacked.iterdir()} == set(ICONSET_SLOTS)
    for name, (edge, scale) in ICONSET_SLOTS.items():
        with Image.open(unpacked / name) as image:
            _assert_master_pixels(image, edge * scale)
    assert hashlib.sha256(master.read_bytes()).hexdigest() == before
