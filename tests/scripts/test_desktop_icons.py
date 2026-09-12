"""Desktop Tauri icon set verification (issue #2307)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from scripts.build_engine_payload import PayloadBuildError
from scripts.desktop_icons import (
    ICNS_REQUIRED_PIXEL_SIZES,
    generate_desktop_icons,
    icns_embedded_pixel_sizes,
    verify_desktop_icons,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ICONS_DIR: Path = REPO_ROOT / "apps/desktop/src-tauri/icons"


@pytest.mark.requirement("INSTALL-17")
def test_committed_icon_set_is_complete() -> None:
    """[if] any Tauri slot is missing [then] payload verify must refuse the build."""
    verify_desktop_icons(ICONS_DIR)


@pytest.mark.requirement("INSTALL-17")
def test_incomplete_icon_set_is_refused(tmp_path: Path) -> None:
    icons = tmp_path / "icons"
    icons.mkdir()
    with pytest.raises(PayloadBuildError, match="incomplete"):
        verify_desktop_icons(icons)


@pytest.mark.requirement("INSTALL-17")
def test_icns_missing_1024_slot_is_refused(tmp_path: Path) -> None:
    master = tmp_path / "master.png"
    Image.new("RGBA", (1024, 1024), (200, 100, 50, 255)).save(master)
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
    master = tmp_path / "master.png"
    Image.new("RGBA", (1024, 1024), (217, 119, 87, 255)).save(master)
    icons = tmp_path / "icons"
    generate_desktop_icons(master, icons)
    embedded = icns_embedded_pixel_sizes(icons / "icon.icns")
    assert embedded == ICNS_REQUIRED_PIXEL_SIZES
