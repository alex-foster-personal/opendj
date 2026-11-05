"""WCAG 2.x AA contrast check for a skin manifest.

The manifest names its color tokens under ``tokens.color`` and lists the pairs that are
actually drawn on top of each other under ``contrast_pairs`` as ``[fg, bg, role]``, where
role is ``text`` (AA normal text, 4.5:1) or ``ui`` (AA non-text component, 3:1).

Exit 0 when every pair meets its floor, 1 when any pair is below it, 2 when the manifest
is malformed (an unknown token, a bad hex value, or no pairs at all - zero pairs is not a pass).

Usage:
  uv run --locked python -m scripts.skins.contrast_check (private path)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FLOORS = {"text": 4.5, "ui": 3.0}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ManifestError(ValueError):
    pass


def _channel(c8: int) -> float:
    c = c8 / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    if not _HEX.match(hex_color):
        raise ManifestError(f"not a #rrggbb color: {hex_color!r}")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    hi, lo = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check(manifest: dict) -> list[tuple[str, str, str, float, bool]]:
    colors = manifest.get("tokens", {}).get("color", {})
    pairs = manifest.get("contrast_pairs", [])
    if not pairs:
        raise ManifestError("manifest lists no contrast_pairs")
    rows = []
    for fg, bg, role in pairs:
        if fg not in colors or bg not in colors:
            raise ManifestError(f"unknown token in pair {fg}/{bg}")
        if role not in FLOORS:
            raise ManifestError(f"unknown role {role!r}")
        ratio = contrast_ratio(colors[fg], colors[bg])
        rows.append((fg, bg, role, round(ratio, 2), ratio >= FLOORS[role]))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        rows = check(json.loads(args.manifest.read_text()))
    except ManifestError as exc:
        print(f"[ERROR] {args.manifest}: {exc}")
        return 2
    for fg, bg, role, ratio, ok in rows:
        print(f"[{'OK' if ok else 'FAIL'}] {fg} on {bg} ({role}): {ratio}:1, floor {FLOORS[role]}:1")
    failed = [r for r in rows if not r[4]]
    print(f"[{'FAIL' if failed else 'OK'}] {len(rows) - len(failed)}/{len(rows)} pairs meet WCAG AA")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
