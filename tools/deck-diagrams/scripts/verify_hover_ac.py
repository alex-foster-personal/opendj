#!/usr/bin/env python3
"""AC gate: every layout control appears in deck.html with hover MIDI attrs + JS."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REQUIRED_ATTRS = (
    "data-fig",
    "data-label",
    "data-midi-name",
    "data-midi-type",
    "data-midi-ch",
    "data-midi-code",
)


def verify_device(device_dir: Path) -> tuple[bool, str]:
    layout_path = device_dir / "layout.json"
    deck_path = device_dir / "deck.html"
    if not layout_path.is_file():
        return False, "missing layout.json"
    if not deck_path.is_file():
        return False, "missing deck.html"
    layout = json.loads(layout_path.read_text())
    controls = layout.get("controls") or []
    if layout.get("blocked"):
        return True, f"skip blocked:{layout.get('blocked')}"
    if not controls:
        return False, "zero controls"
    html = deck_path.read_text()
    if "pointerenter" not in html and "addEventListener('pointerenter'" not in html:
        # generate_html uses pointerenter
        if "pointerenter" not in html:
            return False, "deck.html missing pointerenter hover handler"
    if "id=\"readout\"" not in html and "id='readout'" not in html:
        return False, "deck.html missing #readout"
    missing_figs: list[str] = []
    bad_attrs: list[str] = []
    # Split on control div opens; attrs must not rely on [^>]* (labels may contain '>')
    chunks = re.split(r'(?=<div class="control\b)', html)
    by_fig: dict[str, str] = {}
    for ch in chunks:
        if not ch.startswith('<div class="control'):
            continue
        # take until first '>' that closes the opening tag: after last ="..."
        # Prefer matching full opening tag with quoted attrs only.
        m = re.match(r'<div\b(?:\s+[\w:-]+="[^"]*")*\s*>', ch)
        tag = m.group(0) if m else ch.split(">", 1)[0] + ">"
        fm = re.search(r'data-fig="([^"]*)"', tag)
        if fm:
            by_fig[fm.group(1)] = tag
    for c in controls:
        fig = str(c["fig"])
        tag = by_fig.get(fig)
        if not tag:
            missing_figs.append(fig)
            continue
        for attr in REQUIRED_ATTRS:
            if f'{attr}="' not in tag:
                bad_attrs.append(f"{fig}:{attr}")
    if missing_figs or bad_attrs:
        return False, f"missing_figs={missing_figs[:8]} bad_attrs={bad_attrs[:8]} n={len(controls)}"
    return True, f"ok controls={len(controls)}"


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1] / "devices"
    if len(argv) > 1:
        dirs = [Path(a) for a in argv[1:]]
    else:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    failed = 0
    ok = 0
    for d in dirs:
        good, msg = verify_device(d)
        status = "PASS" if good else "FAIL"
        print(f"{status} {d.name}: {msg}")
        if good:
            ok += 1
        else:
            failed += 1
    print(f"SUMMARY ok={ok} fail={failed} total={ok+failed}")
    # AC for scale: require ok devices; fail if any non-stub failed when args given
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
