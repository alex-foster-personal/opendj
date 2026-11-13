"""requirement: SKIN-14603 (skin package asset sha256 pins and size caps)

[if] a skin asset is unpinned, mispinned, missing, oversized or escaping [then] broken, [else stop].
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.skins.asset_pins import check_asset_pins

pytestmark = pytest.mark.requirement("SKIN-14603")

CAPS = {"svg": 100, "png": 100, "woff2": 100}


def _pkg(tmp_path: Path, name: str = "skin/panel.svg", data: bytes = b"<svg/>") -> dict[str, str]:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return {name: hashlib.sha256(data).hexdigest()}


def test_clean_package_passes(tmp_path: Path) -> None:
    """[if] every file is pinned and within caps [then] no errors, [else stop]."""
    assert check_asset_pins(tmp_path, _pkg(tmp_path), CAPS) == []


def test_sha_mismatch_names_file(tmp_path: Path) -> None:
    """[if] a pin differs from the file sha256 [then] it fails naming the file, [else stop]."""
    files = _pkg(tmp_path)
    files["skin/panel.svg"] = "0" * 64
    assert check_asset_pins(tmp_path, files, CAPS) == ["sha256 mismatch: skin/panel.svg"]


def test_unpinned_file_named(tmp_path: Path) -> None:
    """[if] a skin/ file is not listed [then] it fails naming it unpinned, [else stop]."""
    files = _pkg(tmp_path)
    _pkg(tmp_path, "skin/extra.png", b"x")
    assert check_asset_pins(tmp_path, files, CAPS) == ["unpinned file: skin/extra.png"]


def test_missing_file_named(tmp_path: Path) -> None:
    """[if] a listed file is missing [then] it fails naming it, [else stop]."""
    files = {"skin/gone.svg": "0" * 64}
    assert check_asset_pins(tmp_path, files, CAPS) == ["missing listed file: skin/gone.svg"]


@pytest.mark.parametrize("ext", ["svg", "png", "woff2"])
def test_size_cap_names_file_size_cap(tmp_path: Path, ext: str) -> None:
    """[if] a file exceeds its extension cap [then] it fails with file, size and cap, [else stop]."""
    files = _pkg(tmp_path, f"skin/big.{ext}", b"a" * 101)
    errs = check_asset_pins(tmp_path, files, CAPS)
    assert errs == [f"over size cap: skin/big.{ext} is 101 bytes, cap 100"]


@pytest.mark.parametrize("rel", ["../outside.svg", "/etc/passwd", "skin/../../x.png"])
def test_escaping_path_named(tmp_path: Path, rel: str) -> None:
    """[if] a listed path escapes the package [then] it fails naming the path, [else stop]."""
    assert check_asset_pins(tmp_path, {rel: "0" * 64}, CAPS) == [f"path escapes package: {rel}"]
