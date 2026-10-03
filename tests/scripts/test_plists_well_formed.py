"""Every tracked .plist must be well-formed XML, because codesign parses entitlements strictly.

plutil accepts a comment containing two adjacent hyphens; codesign's AMFI parser does not, and
refuses to sign: "Failed to parse entitlements: AMFIUnserializeXML: syntax error near line 19"
(demon-llama, Sat 3 Oct 2026, main at 423376f7d, after SET-10 added a commented
Entitlements.app.plist). The dmg build then fails at the bundle step, so no signed build of main
was possible. Python's expat is strict in the same way, so it stands in for AMFI here.

- [if] a tracked plist has a double hyphen inside an XML comment [then] this test fails naming it
- [if] the repo tracks no plists [then] this test fails rather than passing on nothing
"""

from __future__ import annotations

import subprocess
import xml.dom.minidom
from pathlib import Path
from xml.parsers.expat import ExpatError

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _tracked_plists() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.plist"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [ROOT / line for line in out.splitlines() if line]


def test_repo_tracks_the_signing_plists() -> None:
    names = {p.name for p in _tracked_plists()}
    assert {"Entitlements.app.plist", "Info.plist"} <= names, names


@pytest.mark.parametrize("plist", _tracked_plists(), ids=lambda p: str(p.relative_to(ROOT)))
def test_plist_is_well_formed_xml(plist: Path) -> None:
    try:
        xml.dom.minidom.parse(str(plist))
    except ExpatError as exc:
        pytest.fail(f"{plist.relative_to(ROOT)} is not well-formed XML ({exc}); codesign will refuse it")


def test_double_hyphen_in_comment_is_rejected() -> None:
    bad = '<?xml version="1.0"?><!-- re-seal with --preserve-metadata --><plist version="1.0"><dict/></plist>'
    with pytest.raises(ExpatError):
        xml.dom.minidom.parseString(bad)
