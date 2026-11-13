"""requirement: SKIN-14187

if an unsafe construct in a skin SVG is not reported, or a DOCTYPE/entity file is parsed then broken
if ok-knob.svg returns any violation then broken
"""

from __future__ import annotations

from pathlib import Path

from scripts.skins.svg_safety import check_svg_asset

FIX = Path(__file__).parent / "fixtures" / "svg"
NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def _svg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "t.svg"
    p.write_text(f"<svg {NS}>\n{body}\n</svg>\n")
    return p


def test_script_reported_with_name_and_line():
    vs = check_svg_asset(FIX / "bad-script.svg")
    assert [(v.rule, v.detail, v.line) for v in vs] == [("element", "script", 2)]


def test_foreign_object_reported(tmp_path):
    vs = check_svg_asset(_svg(tmp_path, "<foreignObject/>"))
    assert [(v.detail, v.line) for v in vs] == [("foreignObject", 2)]


def test_external_href_reported(tmp_path):
    vs = check_svg_asset(_svg(tmp_path, '<use xlink:href="http://x/a.svg#d"/><a href="evil.html"/><use href="#ok"/>'))
    assert [v.rule for v in vs] == ["href", "href"]
    assert "http://x/a.svg#d" in vs[0].detail and "evil.html" in vs[1].detail


def test_event_handler_and_style_reported(tmp_path):
    body = '<rect onclick="x()"/><rect style="fill:url(http://x/p)"/><style>@import "a.css";</style><rect style="fill:url(#g)"/>'
    vs = check_svg_asset(_svg(tmp_path, body))
    assert [v.rule for v in vs] == ["event-handler", "style-url", "style-import"]


def test_doctype_entity_refused_without_expansion(tmp_path):
    p = tmp_path / "e.svg"
    p.write_text('<?xml version="1.0"?>\n<!DOCTYPE svg [<!ENTITY x "boom">]>\n<svg>&x;</svg>\n')
    vs = check_svg_asset(p)
    assert [(v.rule, v.line) for v in vs] == [("doctype", 2)]


def test_ok_knob_clean():
    assert check_svg_asset(FIX / "ok-knob.svg") == []
