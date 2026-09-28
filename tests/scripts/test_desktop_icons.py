"""Desktop Tauri icon set verification (issue #2307)."""

from __future__ import annotations

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
