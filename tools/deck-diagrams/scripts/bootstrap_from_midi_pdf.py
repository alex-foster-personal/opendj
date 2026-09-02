#!/usr/bin/env python3
"""Bootstrap a devices/<id>/ tree from an official MIDI-list PDF.

Creates source archive, plate crop, midi.json (fig inventory), layout.json
(grid-ish positions), footnotes, overlay.html, deck.html.

Usage:
  uv run python tools/deck-diagrams/scripts/bootstrap_from_midi_pdf.py \
    --id ddj-flx4 --model DDJ-FLX4 \
    --pdf docs/controller/reference/DDJ-FLX4_MIDI_Message_List_E1.pdf
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
SCRIPTS = Path(__file__).resolve().parent


def _run(cmd: list[str]) -> None:
    subprocess.check_call(cmd)


_FIG_LETTER = re.compile(r"\b([BDFMP]\d{1,2})\b")
# Pioneer FLX4/FLX6 style plate labels: 1-1, 3-15, 5-4
_FIG_SECTION = re.compile(r"\b([1-9]-\d{1,2})\b")


def _fig_sort_key(fig: str) -> tuple:
    if "-" in fig:
        a, b = fig.split("-", 1)
        return (0, int(a), int(b))
    return (1, fig[0], int(fig[1:]))


def _extract_figs(text: str) -> list[str]:
    """Extract plate fig ids. Supports letter (D1) and section (1-1) styles."""
    head = text[:16000]
    letter = set(_FIG_LETTER.findall(head))
    section = set(_FIG_SECTION.findall(head))
    # Prefer the denser scheme on the plate page
    if len(section) >= len(letter) and len(section) >= 8:
        figs = section
    elif letter:
        figs = letter
    else:
        figs = set(_FIG_LETTER.findall(text)) | set(_FIG_SECTION.findall(text))
    return sorted(figs, key=_fig_sort_key)


def _grid_pos(figs: list[str]) -> dict[str, tuple[float, float, float, float, str]]:
    """Deterministic fallback positions so overlay is non-empty; refine by hand."""
    pos: dict[str, tuple[float, float, float, float, str]] = {}
    cols = 10
    for i, fig in enumerate(figs):
        col = i % cols
        row = i // cols
        kind = "button"
        if fig.startswith("P") and fig[1:].isdigit() and int(fig[1:]) <= 8:
            kind = "pad"
        if fig.startswith("B"):
            kind = "encoder"
        pos[fig] = (3 + col * 9.5, 8 + row * 10, 4.5, 5.5, kind)
    return pos


def bootstrap(device_id: str, model: str, pdf: Path, vendor: str) -> Path:
    dev = ROOT / "devices" / device_id
    src = dev / "source"
    src.mkdir(parents=True, exist_ok=True)
    dest_pdf = src / pdf.name
    if pdf.resolve() != dest_pdf.resolve():
        shutil.copy2(pdf, dest_pdf)

    # render pages
    prefix = src / "midi-list-page"
    _run(["pdftoppm", "-png", "-r", "140", str(dest_pdf), str(prefix)])
    pages = sorted(src.glob("midi-list-page-*.png"))
    if not pages:
        raise SystemExit(f"no pages rendered for {model}")

    from PIL import Image

    im = Image.open(pages[0])
    w, h = im.size
    plate = im.crop((20, 40, w - 20, int(h * 0.40)))
    plate_path = src / "plate-top.png"
    plate.save(plate_path)

    txt_path = src / (pdf.stem + ".txt")
    _run(["pdftotext", "-layout", str(dest_pdf), str(txt_path)])
    text = txt_path.read_text(errors="replace")
    figs = _extract_figs(text)
    if len(figs) < 5:
        # fallback: denser scan
        figs = sorted(set(re.findall(r"\b([BDFMP]\d{1,2})\b", text)), key=lambda x: (x[0], int(x[1:])))

    controls_midi = []
    for i, fig in enumerate(figs):
        controls_midi.append(
            {
                "name": f"fig_{fig.lower()}",
                "section": "unknown",
                "type": "note",
                "ch": 1,
                "code": i % 128,
                "notes": f"bootstrap stub from PDF plate label [{fig}] - refine from tables",
            }
        )

    midi = {
        "source": pdf.resolve().relative_to(REPO_ROOT).as_posix(),
        "archived": dest_pdf.relative_to(REPO_ROOT).as_posix(),
        "device": model,
        "vendor": vendor,
        "bootstrap": True,
        "controls": controls_midi,
    }
    (dev / "midi.json").write_text(json.dumps(midi, indent=2) + "\n")

    pos = _grid_pos(figs)
    layout_controls = []
    for fig in figs:
        x, y, w_, h_, kind = pos[fig]
        layout_controls.append(
            {
                "id": f"{device_id}-{fig.lower()}",
                "fig": fig,
                "label": fig,
                "kind": kind,
                "x": x,
                "y": y,
                "w": w_,
                "h": h_,
                "layer": "base",
                "section": "unknown",
                "midi": {
                    "name": f"fig_{fig.lower()}",
                    "type": "note",
                    "ch": 1,
                    "code": 0,
                    "shift_name": None,
                },
                "notes": f"[{fig}] bootstrap - refine coords against plate-top.png",
            }
        )
        # fix code to match midi
        for c in controls_midi:
            if c["name"] == f"fig_{fig.lower()}":
                layout_controls[-1]["midi"]["code"] = c["code"]
                break

    layout = {
        "device": model,
        "vendor": vendor,
        "plate": {"image": "source/plate-top.png", "width": plate.size[0], "height": plate.size[1]},
        "layers": ["base", "shift"],
        "out_of_scope": ["hid_screens", "refine_shift_twins_pending"],
        "controls": layout_controls,
        "bootstrap": True,
    }
    (dev / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")

    (dev / "footnotes.md").write_text(
        f"""# {model} diagram footnotes

Bootstrapped from official MIDI message list PDF.

## Status

- Plate cropped from PDF page 1 → `source/plate-top.png`
- Fig inventory extracted heuristically ({len(figs)} labels)
- MIDI codes in `midi.json` are **stubs** until table parse is completed
- Positions are a grid fallback - refine in red overlay against the plate
- SHIFT twins not yet joined (see skill workflow step 5)

## Next (skill)

1. Parse MIDI tables into real `midi.json` rows with `[Fig]` tags
2. Hand-tune `layout.json` x/y against plate (red overlay)
3. Wire `shift_name` twins
4. `uv run python tools/deck-diagrams/scripts/checksum.py tools/deck-diagrams/devices/{device_id}`
"""
    )

    _run(["uv", "run", "python", str(SCRIPTS / "generate_html.py"), str(dev)])
    print(f"bootstrapped {device_id}: {len(figs)} figs → {dev}")
    return dev


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument("--vendor", default="AlphaTheta / Pioneer DJ")
    args = ap.parse_args()
    if not args.pdf.is_file():
        raise SystemExit(f"missing pdf: {args.pdf}")
    bootstrap(args.id, args.model, args.pdf, args.vendor)


if __name__ == "__main__":
    main()
