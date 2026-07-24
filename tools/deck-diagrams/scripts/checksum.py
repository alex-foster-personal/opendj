#!/usr/bin/env python3
"""Checksum layout.json figs against midi.json [Fig] tags + shift twins."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_FIG_RE = re.compile(r"\[([BDEFMP]\d{1,2}|[1-9]-\d{1,2}|[a-z][a-z0-9_]{1,40})\]")


def figs_in_midi(midi: dict) -> set[str]:
    out: set[str] = set()
    for c in midi.get("controls", []):
        m = _FIG_RE.search(c.get("notes", "") or "")
        if m:
            out.add(m.group(1))
    return out


def names_in_midi(midi: dict) -> set[str]:
    return {c["name"] for c in midi.get("controls", [])}


def main(device_dir: Path) -> int:
    midi = json.loads((device_dir / "midi.json").read_text())
    layout = json.loads((device_dir / "layout.json").read_text())
    footnotes = device_dir / "footnotes.md"
    waived: set[str] = set()
    if footnotes.exists():
        for line in footnotes.read_text().splitlines():
            if line.startswith("WAIVE_FIG:"):
                waived.add(line.split(":", 1)[1].strip())

    midi_figs = figs_in_midi(midi)
    layout_figs = {c["fig"] for c in layout["controls"]}
    names = names_in_midi(midi)

    def _sk(x: str) -> tuple:
        if "-" in x and x[0].isdigit():
            a, b = x.split("-", 1)
            return (0, int(a), int(b))
        if len(x) >= 2 and x[0].isalpha() and x[1:].isdigit():
            return (1, x[0], int(x[1:]))
        return (2, x)

    missing = sorted(midi_figs - layout_figs - waived, key=_sk)
    extra = sorted(layout_figs - midi_figs, key=_sk)

    bad_shift: list[str] = []
    bad_midi: list[str] = []
    for c in layout["controls"]:
        m = c["midi"]
        if m["name"] not in names:
            bad_midi.append(f"{c['fig']}:{m['name']}")
        sn = m.get("shift_name")
        if sn and sn not in names:
            bad_shift.append(f"{c['fig']}:{sn}")

    print(f"device={layout.get('device')} midi_figs={len(midi_figs)} layout_figs={len(layout_figs)}")
    print(f"missing_in_layout={len(missing)} extra_in_layout={len(extra)} bad_midi={len(bad_midi)} bad_shift={len(bad_shift)}")
    if missing:
        print("MISSING:", ", ".join(missing))
    if extra:
        print("EXTRA:", ", ".join(extra))
    if bad_midi:
        print("BAD_MIDI_NAME:", ", ".join(bad_midi))
    if bad_shift:
        print("BAD_SHIFT_NAME:", ", ".join(bad_shift))

    ok = not missing and not extra and not bad_midi and not bad_shift
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: checksum.py <device_dir>", file=sys.stderr)
        sys.exit(2)
    raise SystemExit(main(Path(sys.argv[1])))
