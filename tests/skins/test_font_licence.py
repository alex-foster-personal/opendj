"""requirement: SKIN-14373

if a font token names a family that is neither bundled nor packaged and passes then broken
if a packaged woff2 with no or empty licence passes then broken
if a packaged woff2 with a licence outside the allowlist passes then broken
if bundled and openly licensed packaged fonts report errors then broken
"""

from __future__ import annotations

from scripts.skins.font_licence import check_skin_fonts

BUNDLED = ["Inter"]


def test_unknown_family_fails_naming_token_and_family() -> None:
    errors = check_skin_fonts({"font.body": "Helvetica Neue"}, [], BUNDLED)
    assert len(errors) == 1
    assert "font.body" in errors[0] and "Helvetica Neue" in errors[0]


def test_missing_or_empty_licence_fails_naming_file() -> None:
    assets = [{"file": "a.woff2", "family": "A"}, {"file": "b.woff2", "family": "B", "licence": " "}]
    errors = check_skin_fonts({}, assets, BUNDLED)
    assert any("a.woff2" in e for e in errors)
    assert any("b.woff2" in e for e in errors)


def test_disallowed_licence_fails_naming_file_and_licence() -> None:
    assets = [{"file": "pro.woff2", "family": "Pro", "licence": "Proprietary"}]
    errors = check_skin_fonts({"font.title": "Pro"}, assets, BUNDLED)
    assert any("pro.woff2" in e and "Proprietary" in e for e in errors)
    assert any("font.title" in e for e in errors)


def test_all_bundled_or_open_packaged_passes() -> None:
    assets = [{"file": "fonts/mono.woff2", "family": "Mono", "licence": "OFL-1.1"}]
    tokens = {"font.body": "Inter", "font.mono": "Mono", "color.bg": "#000000"}
    assert check_skin_fonts(tokens, assets, BUNDLED) == []
