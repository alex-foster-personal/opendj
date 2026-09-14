"""The dmg recipe must re-sign the app AFTER the Icon Composer install and BEFORE notarizing.

`cargo tauri build` signs the bundle; the icon step then rewrites Contents/Resources and
Info.plist, so without a re-seal the notary service answers Invalid (Air, Mon 14 Sep 2026:
submissions d3812d76 at f10129948 and d96ae9e7 at b2ff18aeb). #2635 added the re-seal and a
later merge dropped it silently; this test pins the ordering in the justfile text.

- [if] the re-seal block is missing [then] this test fails naming the missing marker
- [if] the re-seal sits before the icon install or after notarize start [then] this test fails
"""

from __future__ import annotations

from pathlib import Path

JUSTFILE = Path(__file__).resolve().parents[2] / "justfile"

ICON_INSTALLED = 'echo "[TIMING] icon_composer_asset_installed'
RESIGN = 'codesign --force --timestamp --options runtime --sign "$identity" "$source_app"'
VERIFY = 'codesign --verify --deep --strict "$source_app"'
RESEALED = 'echo "[TIMING] app_resealed_after_icon'
NOTARIZE_START = 'echo "[TIMING] app_notarize_start'


def _index_of(text: str, marker: str) -> int:
    idx = text.find(marker)
    assert idx >= 0, f"justfile no longer contains the marker: {marker}"
    return idx


def test_dmg_recipe_reseals_the_app_between_icon_install_and_notarization() -> None:
    text = JUSTFILE.read_text(encoding="utf-8")
    icon = _index_of(text, ICON_INSTALLED)
    resign = _index_of(text, RESIGN)
    verify = _index_of(text, VERIFY)
    resealed = _index_of(text, RESEALED)
    notarize = _index_of(text, NOTARIZE_START)
    assert icon < resign < verify < resealed < notarize, (
        "re-seal must follow the icon install and precede notarization: "
        f"icon={icon} resign={resign} verify={verify} resealed={resealed} notarize={notarize}"
    )


def test_dmg_recipe_resign_is_not_deep() -> None:
    """Nested code keeps its own signatures; a --deep re-sign would clobber them."""
    text = JUSTFILE.read_text(encoding="utf-8")
    resign_line = next(line for line in text.splitlines() if RESIGN in line)
    assert "--deep" not in resign_line, resign_line
