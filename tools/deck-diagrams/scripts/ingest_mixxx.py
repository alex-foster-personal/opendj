#!/usr/bin/env python3
"""Ingest tier-2 deck diagrams from Mixxx res/controllers/*.midi.xml.

Creates devices/<id>/ with midi.json, layout.json, deck.html, overlay.html,
footnotes.md. Positions are a deterministic grid until a plate is added.

Usage:
  uv run python tools/deck-diagrams/scripts/ingest_mixxx.py --id numark-ns6ii
  uv run python tools/deck-diagrams/scripts/ingest_mixxx.py --id roland-dj-505 --xml /path/to/map.midi.xml
  uv run python tools/deck-diagrams/scripts/ingest_mixxx.py --all
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
SCRIPTS = Path(__file__).resolve().parent
CAT = ROOT / "catalog" / "controllers.json"
DEFAULT_MIXXX = REPO / ".tmp" / "mixxx-controllers" / "res" / "controllers"

# catalog id -> Mixxx res/controllers basename (None = no .midi.xml in upstream Mixxx)
MIXXX_XML: dict[str, str | None] = {
    "traktor-s2-mk3": None,  # HID-only in Mixxx
    "traktor-s3": None,
    "traktor-s4-mk3": None,
    "numark-mixtrack-platinum-fx": "Numark Mixtrack Platinum FX.midi.xml",
    "numark-mixtrack-pro-fx": "Numark Mixtrack Pro FX.midi.xml",
    "hercules-inpulse-500": "Hercules_DJControl_Inpulse_500.midi.xml",
    "roland-dj-202": None,
    "roland-dj-505": "Roland_DJ-505.midi.xml",
    "denon-prime4": None,
    "denon-sc6000": None,
    "rane-one": None,
    "reloop-mixon-8": None,
    "traktor-x1-mk2": "Traktor Kontrol X1.midi.xml",
    "numark-ns6ii": "Numark NS6II.midi.xml",
    "hercules-inpulse-300": "Hercules_DJControl_Inpulse_300.midi.xml",
}


@dataclass
class ParsedControl:
    name: str
    label: str
    section: str
    kind: str
    midi_type: str
    ch: int
    code: int
    group: str
    key: str
    description: str


def _hex_byte(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw.startswith("0x"):
        return int(raw, 16)
    if raw.isdigit():
        return int(raw)
    return None


def _camel_to_snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text or "control"


def _guess_section(group: str) -> str:
    g = group.lower()
    if "channel" in g or "deck" in g:
        return "deck"
    if "library" in g or "browse" in g:
        return "browser"
    if "master" in g or "mixer" in g or "crossfader" in g:
        return "mixer"
    if "effect" in g:
        return "effect"
    if "sampler" in g:
        return "sampler"
    return "other"


def _guess_kind(key: str, description: str, midi_type: str) -> str:
    blob = f"{key} {description}".lower()
    if any(x in blob for x in ("jog", "platter", "wheel", "vinyl")):
        return "jog"
    if any(x in blob for x in ("crossfader", "fader", "tempo", "volume", "gain")):
        return "fader"
    if "pad" in blob or "hotcue" in blob:
        return "pad"
    if any(x in blob for x in ("encoder", "browse", "encoder")):
        return "encoder"
    if any(x in blob for x in ("knob", "eq", "filter", "trim", "rate")):
        return "knob"
    if midi_type == "cc":
        return "knob"
    return "button"


def _status_to_midi(status: int, midino: int) -> tuple[str, int, int] | None:
    hi = status >> 4
    ch = (status & 0x0F) + 1
    if hi in (8, 9):
        return "note", ch, midino
    if hi == 11:
        return "cc", ch, midino
    if hi == 10:
        return "cc", ch, midino
    if hi == 14:
        return "cc", ch, midino
    return None


def _name_from_control(key: str, description: str, group: str) -> tuple[str, str]:
    label = (description or "").strip()
    if not label:
        label = key.split(".")[-1] if key else group
    label = re.sub(r"\s+", " ", label).strip()
    base = key.split(".")[-1] if key else label
    name = _camel_to_snake(base)
    if name in ("input", "inputmsb", "inputlsb", "output"):
        name = _camel_to_snake(f"{group}_{label}" if label else f"{group}_{base}")
    return name, label or name


def _load_catalog() -> dict[str, dict]:
    data = json.loads(CAT.read_text())
    return {c["id"]: c for c in data["controllers"]}


def parse_mixxx_xml(xml_path: Path) -> tuple[str, list[ParsedControl]]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    device_name = ""
    info_name = root.find("./info/name")
    if info_name is not None and info_name.text:
        device_name = info_name.text.strip()

    seen: set[tuple[str, int, int]] = set()
    used_names: set[str] = set()
    controls: list[ParsedControl] = []

    for el in root.findall(".//control"):
        status = _hex_byte(el.findtext("status"))
        midino = _hex_byte(el.findtext("midino"))
        if status is None or midino is None:
            continue
        parsed = _status_to_midi(status, midino)
        if not parsed:
            continue
        midi_type, ch, code = parsed
        dedupe = (midi_type, ch, code)
        if dedupe in seen:
            continue
        seen.add(dedupe)

        group = (el.findtext("group") or "").strip()
        key = (el.findtext("key") or "").strip()
        description = (el.findtext("description") or "").strip()
        name, label = _name_from_control(key, description, group)
        if name in used_names:
            suffix = 2
            while f"{name}_{suffix}" in used_names:
                suffix += 1
            name = f"{name}_{suffix}"
        used_names.add(name)

        controls.append(
            ParsedControl(
                name=name,
                label=label,
                section=_guess_section(group),
                kind=_guess_kind(key, description, midi_type),
                midi_type=midi_type,
                ch=ch,
                code=code,
                group=group,
                key=key,
                description=description,
            )
        )
    return device_name, controls


def _grid_pos(n: int, kind: str, index: int) -> tuple[float, float, float, float]:
    cols = 12
    col = index % cols
    row = index // cols
    w, h = 4.0, 4.5
    if kind == "fader":
        w, h = 3.0, 8.0
    elif kind == "jog":
        w = h = 6.0
    elif kind == "pad":
        w = h = 3.5
    return (2.0 + col * 7.8, 4.0 + row * 6.5, w, h)


def _shift_pairs(names: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {n: None for n in names}
    by_base: dict[str, str] = {}
    for n in names:
        if n.endswith("_shift"):
            base = n[: -len("_shift")]
            by_base[base] = n
    for n in names:
        if n.endswith("_shift"):
            continue
        twin = by_base.get(n)
        if twin:
            out[n] = twin
    return out


def ingest(
    device_id: str,
    xml_path: Path,
    *,
    model: str | None = None,
    vendor: str | None = None,
    mixxx_rel: str = "",
) -> int:
    parsed_name, controls = parse_mixxx_xml(xml_path)
    if not controls:
        raise SystemExit(f"no MIDI controls parsed from {xml_path}")

    cat = _load_catalog()
    entry = cat.get(device_id, {})
    device = model or entry.get("model") or parsed_name or device_id
    brand = vendor or entry.get("brand") or "unknown"

    dev = ROOT / "devices" / device_id
    src = dev / "source"
    src.mkdir(parents=True, exist_ok=True)
    dest_xml = src / xml_path.name
    if xml_path.resolve() != dest_xml.resolve():
        shutil.copy2(xml_path, dest_xml)

    midi_controls = []
    for c in controls:
        midi_controls.append(
            {
                "name": c.name,
                "section": c.section,
                "type": c.midi_type,
                "ch": c.ch,
                "code": c.code,
                "notes": f"Mixxx {c.key or c.description} [{c.name}]",
            }
        )

    midi = {
        "source": f"mixxx:{mixxx_rel or xml_path.name}",
        "archived": str(dest_xml.relative_to(REPO)),
        "device": device,
        "vendor": brand,
        "doc_tier": 2,
        "mixxx_gpl": True,
        "controls": midi_controls,
    }
    (dev / "midi.json").write_text(json.dumps(midi, indent=2) + "\n")

    shift_map = _shift_pairs([c.name for c in controls])
    layout_controls = []
    for i, c in enumerate(controls):
        x, y, w, h = _grid_pos(len(controls), c.kind, i)
        layout_controls.append(
            {
                "id": f"{device_id}-{c.name}",
                "fig": c.name,
                "label": c.label,
                "kind": c.kind,
                "x": round(x, 2),
                "y": round(y, 2),
                "w": w,
                "h": h,
                "layer": "base",
                "section": c.section,
                "midi": {
                    "name": c.name,
                    "type": c.midi_type,
                    "ch": c.ch,
                    "code": c.code,
                    "shift_name": shift_map.get(c.name),
                },
                "notes": f"[{c.name}] Mixxx ingest - refine coords against plate",
            }
        )

    layout = {
        "device": device,
        "vendor": brand,
        "plate": {"image": "", "width": 0, "height": 0},
        "layers": ["base", "shift"],
        "out_of_scope": ["mixxx_script_bindings", "hid_surfaces", "plate_pending"],
        "controls": layout_controls,
    }
    (dev / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")

    mixxx_url = "https://github.com/mixxxdj/mixxx/blob/main/res/controllers/" + (
        mixxx_rel or xml_path.name
    ).replace(" ", "%20")
    (dev / "footnotes.md").write_text(
        f"""# {device} diagram footnotes

Ingested from Mixxx controller mapping (GPL-2.0-or-later).

## Source

- Mixxx file: `{mixxx_rel or xml_path.name}`
- Upstream: {mixxx_url}
- Local archive: `{dest_xml.relative_to(REPO)}`

## Status

- {len(controls)} controls parsed from Mixxx `<control>` status/midino entries
- Positions are an auto grid only - **spot-check required** against hardware or photos
- No official vendor MIDI PDF; do not treat as checksum-validated tier-1
- SHIFT twins inferred only when Mixxx names end with `_shift`
- Script-bound and HID-only surfaces may be incomplete vs real hardware

## License note

Mixxx mappings are GPL. This diagram consumes MIDI numbers for teaching/reference;
retain GPL notice if redistributing derived mapping work.
"""
    )

    subprocess.check_call(["uv", "run", "python", str(SCRIPTS / "generate_html.py"), str(dev)])
    print(f"ingested {device_id}: {len(controls)} controls -> {dev}")
    return len(controls)


def _resolve_xml(device_id: str, xml_arg: Path | None, mixxx_dir: Path) -> tuple[Path, str] | None:
    if xml_arg:
        if not xml_arg.is_file():
            raise SystemExit(f"missing xml: {xml_arg}")
        return xml_arg, xml_arg.name
    basename = MIXXX_XML.get(device_id)
    if basename is None:
        return None
    path = mixxx_dir / basename
    if not path.is_file():
        raise SystemExit(f"Mixxx xml not found: {path}")
    return path, basename


def _patch_catalog(device_id: str, control_count: int) -> None:
    cat = json.loads(CAT.read_text())
    for c in cat["controllers"]:
        if c["id"] != device_id:
            continue
        st = c.setdefault("status", {})
        st["docs"] = "mixxx"
        st["midi"] = "ok"
        st["diagram"] = "ok"
        st["checksum"] = "n/a"
        st["hover"] = "ok"
        notes = c.get("popularity_notes", "")
        notes = re.sub(r"; mixxx ingest \d+ ctrls", "", notes).strip()
        c["popularity_notes"] = f"{notes}; mixxx ingest {control_count} ctrls".strip("; ")
        break
    cat["updated"] = "2026-07-24"
    CAT.write_text(json.dumps(cat, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest tier-2 deck diagrams from Mixxx MIDI XML")
    ap.add_argument("--id", help="catalog device id")
    ap.add_argument("--xml", type=Path, help="path to Mixxx .midi.xml (overrides built-in map)")
    ap.add_argument("--mixxx-dir", type=Path, default=DEFAULT_MIXXX, help="Mixxx res/controllers dir")
    ap.add_argument("--all", action="store_true", help="ingest every mapped id with Mixxx xml")
    ap.add_argument("--patch-catalog", action="store_true", default=True)
    ap.add_argument("--no-patch-catalog", action="store_false", dest="patch_catalog")
    args = ap.parse_args()

    if args.all:
        results: list[tuple[str, int]] = []
        skipped: list[str] = []
        for device_id, basename in MIXXX_XML.items():
            if not basename:
                skipped.append(device_id)
                continue
            xml_path = args.mixxx_dir / basename
            if not xml_path.is_file():
                skipped.append(f"{device_id}(missing)")
                continue
            n = ingest(device_id, xml_path, mixxx_rel=basename)
            if args.patch_catalog:
                _patch_catalog(device_id, n)
            results.append((device_id, n))
        print("\nINGEST SUMMARY")
        for did, n in results:
            print(f"  {did}: {n} controls")
        if skipped:
            print(f"skipped (no Mixxx .midi.xml): {', '.join(skipped)}")
        return 0

    if not args.id:
        ap.error("pass --id or --all")
    resolved = _resolve_xml(args.id, args.xml, args.mixxx_dir)
    if resolved is None:
        raise SystemExit(
            f"{args.id}: no Mixxx .midi.xml in built-in map (HID-only or missing upstream)"
        )
    xml_path, rel = resolved
    n = ingest(args.id, xml_path, mixxx_rel=rel)
    if args.patch_catalog:
        _patch_catalog(args.id, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
