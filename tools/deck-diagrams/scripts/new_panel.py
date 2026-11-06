"""Scaffold `panel.json` and `panel.reference.json` for a practice replica (ADR-0137).

Reads wire identities from the committed `DEVICE_MAP_REGISTRY` export
(`apps/audio-engine/maps/device-maps.json`). One control per binding; placeholder
positions on a deterministic grid; `"measured": false` until traced.

Add or correct the manufacturer panel size in `DEVICES` before scaffolding a
new controller.

Run:
  uv run python tools/deck-diagrams/scripts/new_panel.py ddj-400 --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo root: tools/deck-diagrams/scripts/new_panel.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
DEVICES_DIR = ROOT / "tools" / "deck-diagrams" / "devices"
MAPS_EXPORT = ROOT / "apps" / "audio-engine" / "maps" / "device-maps.json"
PANEL_FORMAT = "opendj-panel-layout/1"

_DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_SECTION_ORDER = {"deck": 0, "performance": 1, "mixer": 2, "effect": 3, "browse": 4}

_TYPE_BY_ACTION: dict[str, str] = {
    "deck_pitch": "fader",
    "deck_jog": "jog",
    "mixer_channel": "knob",
    "mixer_global": "knob",
    "headphone_mix": "knob",
    "headphone_level": "knob",
    "browse_encoder": "encoder",
    "deck_play_toggle": "button",
    "deck_cue": "button",
    "shift_modifier": "button",
    "deck_loop_exit": "button",
    "deck_beat_loop": "button",
    "deck_hot_cue": "pad",
    "channel_cue": "button",
    "browse_load": "button",
    "master_cue": "button",
    "deck_sync_toggle": "button",
    "controller_pad_mode": "button",
    "deck_manual_loop_cycle": "button",
    "deck_loop_scale": "button",
    "deck_key_sync_toggle": "button",
    "deck_key_nudge": "button",
    "deck_tempo_nudge": "button",
    "deck_auto_loop_toggle": "button",
    "deck_stem_eq_toggle": "button",
}

_LABELS: dict[str, str] = {
    "deck_play_toggle": "PLAY/PAUSE",
    "deck_cue": "CUE",
    "shift_modifier": "SHIFT",
    "deck_pitch": "TEMPO",
    "deck_beat_loop": "BEAT LOOP",
    "deck_loop_exit": "4 BEAT/EXIT",
    "deck_hot_cue": "HOT CUE",
    "channel_cue": "CH CUE",
    "browse_encoder": "BROWSE",
    "browse_load": "LOAD",
    "master_cue": "MASTER CUE",
    "headphone_mix": "PHONES MIX",
    "headphone_level": "PHONES LEVEL",
}

_MIXER_TARGET_LABEL = {
    "trim": "TRIM",
    "eq": "EQ",
    "fader": "CH FADER",
    "filter": "FILTER",
    "crossfader": "CROSSFADER",
    "master": "MASTER",
}


@dataclass(frozen=True)
class Device:
    map_name: str
    map_const: str
    map_ts: str
    panel_w: float
    panel_d: float
    panel_size_source: str
    display_name: str


# A new controller: add a row here first (checklist step 1). Sizes must match the vendor manual.
DEVICES: dict[str, Device] = {
    "ddj-400": Device(
        map_name="DDJ-400",
        map_const="DDJ400_MAP",
        map_ts="apps/webui/frontend/src/lib/rb/midi/maps/ddj-400.ts",
        panel_w=482.0,
        panel_d=272.4,
        panel_size_source=(
            "DDJ-400 Operating Instructions, Specifications (482 x 272.4 mm); confirm at checklist step 1"
        ),
        display_name="2-deck entry controller",
    ),
    "ddj-flx4": Device(
        map_name="DDJ-FLX4",
        map_const="FLX4_MAP",
        map_ts="apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts",
        panel_w=482.6,
        panel_d=272.4,
        panel_size_source=("DDJ-FLX4 operating instructions, Specifications (482.6 x 272.4 mm)"),
        display_name="2-deck entry controller",
    ),
}


def _slug_segment(raw: str) -> str:
    s = "".join(ch if ch.isalnum() else "-" for ch in raw.lower())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _beats_slug(beats: float | int) -> str:
    return str(beats).replace(".", "-")


def _body_mixer_channel(action: dict[str, Any]) -> str:
    tgt = action["target"]
    if tgt == "eq" and "band" in action:
        return _slug_segment(f"{tgt}-{action['band']}")
    return _slug_segment(str(tgt))


def _body_default(action: dict[str, Any]) -> str:
    t = action["type"]
    return _slug_segment(t.replace("deck_", "").replace("mixer_", ""))


_BODY_BY_TYPE: dict[str, Any] = {
    "mixer_channel": _body_mixer_channel,
    "mixer_global": lambda a: _slug_segment(str(a["target"])),
    "deck_hot_cue": lambda a: _slug_segment(f"hot-cue-{a['slot']}"),
    "deck_beat_loop": lambda a: _slug_segment(f"beat-loop-{_beats_slug(a['beats'])}"),
    "deck_play_toggle": lambda _a: "play-toggle",
    "deck_cue": lambda _a: "cue",
    "deck_pitch": lambda _a: "pitch",
    "deck_loop_exit": lambda _a: "loop-exit",
    "channel_cue": lambda _a: "ch-cue",
    "browse_encoder": lambda _a: "browse-encoder",
    "browse_load": lambda a: _slug_segment(f"load-{a['deck']}"),
    "headphone_mix": lambda _a: "phones-mix",
    "headphone_level": lambda _a: "phones-level",
    "master_cue": lambda _a: "master-cue",
    "shift_modifier": lambda _a: "shift-modifier",
}


def control_body_from_action(action: dict[str, Any]) -> str:
    t = action["type"]
    fn = _BODY_BY_TYPE.get(t)
    if fn is not None:
        return fn(action)
    return _body_default(action)


def control_prefix(action: dict[str, Any]) -> str:
    t = action["type"]
    if t in ("mixer_channel", "channel_cue"):
        deck = action.get("deck")
        if deck is not None:
            return f"ch{deck}"
    if t in (
        "mixer_global",
        "headphone_mix",
        "headphone_level",
        "master_cue",
        "browse_encoder",
        "shift_modifier",
    ):
        return ""
    deck = action.get("deck")
    if deck is not None:
        return f"d{deck}"
    return ""


def provisional_control_id(action: dict[str, Any]) -> str:
    pre = control_prefix(action)
    body = control_body_from_action(action)
    return f"{pre}-{body}" if pre else body


def resolve_id_collisions(bindings: list[dict[str, Any]]) -> list[str]:
    raw = [provisional_control_id(b["action"]) for b in bindings]
    counts = Counter(raw)
    dup = {k for k, v in counts.items() if v > 1}
    out: list[str] = []
    for b, rid in zip(bindings, raw, strict=True):
        src = b["source"]
        suffix = f"c{src['ch']}-{src['kind']}{src['id']}"
        if rid in dup:
            out.append(f"{rid}-{suffix}")
        else:
            out.append(rid)
    counts2 = Counter(out)
    still_dup = [k for k, v in counts2.items() if v > 1]
    if still_dup:
        raise SystemExit(f"[ERROR] duplicate control ids after collision rule: {still_dup!r}", 2)
    return out


def _mapped_control_type(action: dict[str, Any], at: str) -> tuple[str, bool]:
    if at == "mixer_channel":
        if action.get("target") == "fader":
            return "fader", False
        return "knob", False
    if at == "mixer_global":
        if action.get("target") == "crossfader":
            return "crossfader", False
        return "knob", False
    return _TYPE_BY_ACTION[at], False


def control_type(action: dict[str, Any], source: dict[str, Any], pad_channels: set[int]) -> tuple[str, bool]:
    at = action["type"]
    if at in ("deck_hot_cue", "controller_pad"):
        return "pad", False
    if source["ch"] in pad_channels and at == "deck_beat_loop":
        return "pad", False
    if at in _TYPE_BY_ACTION:
        return _mapped_control_type(action, at)
    sk = source["kind"]
    if sk == "note":
        return "button", True
    if sk == "cc":
        return "knob", True
    return "button", True


def section_for(action: dict[str, Any], ctrl_type: str) -> str:
    if ctrl_type == "pad":
        return "performance"
    t = action["type"]
    if "fx" in t:
        return "effect"
    if t in (
        "mixer_channel",
        "mixer_global",
        "channel_cue",
        "headphone_mix",
        "headphone_level",
        "master_cue",
    ):
        return "mixer"
    if t.startswith("browse"):
        return "browse"
    return "deck"


def label_for_action(action: dict[str, Any]) -> str:
    t = action["type"]
    if t in _LABELS:
        base = _LABELS[t]
        if t == "deck_hot_cue":
            return f"{base} {action['slot']}"
        if t == "deck_beat_loop":
            return f"{base} {_beats_slug(action['beats'])}"
        if t == "browse_load":
            return f"{base} {action['deck']}"
        return base
    if t == "mixer_channel":
        tg = action["target"]
        if tg == "eq":
            band = action.get("band", "")
            return f"CH {action['deck']} {_MIXER_TARGET_LABEL['eq']} {band.upper()}"
        return f"CH {action['deck']} {_MIXER_TARGET_LABEL.get(tg, tg.upper())}"
    if t == "mixer_global":
        tg = action["target"]
        return _MIXER_TARGET_LABEL.get(tg, tg.upper())
    return t.replace("_", " ").upper()


def does_line(action: dict[str, Any], map_const: str) -> str:
    t = action["type"]
    rest = {k: v for k, v in action.items() if k != "type"}
    return f"{t} {json.dumps(rest, sort_keys=True)} via {map_const} (draft: rewrite at the bind step)"


def load_map(device: Device, export: Path = MAPS_EXPORT) -> dict[str, Any]:
    payload = json.loads(export.read_text())
    maps = [m for m in payload["maps"] if m.get("nameMatch") == device.map_name]
    if len(maps) != 1:
        raise SystemExit(
            f"[ERROR] expected exactly one map with nameMatch {device.map_name!r}, got {len(maps)}",
            2,
        )
    return maps[0]


def pad_channels_from_bindings(bindings: list[dict[str, Any]]) -> set[int]:
    return {b["source"]["ch"] for b in bindings if b["action"]["type"] == "deck_hot_cue"}


def placeholder_grid(n: int, panel_w: float, panel_d: float) -> list[tuple[float, float, float, float]]:
    if n <= 0:
        return []
    cols = math.ceil(math.sqrt(n * panel_w / panel_d))
    rows = math.ceil(n / cols)
    cell_w = panel_w / cols
    cell_h = panel_d / rows
    out: list[tuple[float, float, float, float]] = []
    for i in range(n):
        col, row = i % cols, i // cols
        x0 = col * cell_w + 1.0
        y0 = row * cell_h + 1.0
        w0 = max(cell_w - 2.0, 1.0)
        h0 = max(cell_h - 2.0, 1.0)
        x = math.floor(x0 * 2) / 2
        y = math.floor(y0 * 2) / 2
        w = math.floor(w0 * 2) / 2
        h = math.floor(h0 * 2) / 2
        out.append((x, y, w, h))
    return out


def build(
    device_id: str,
    panel_size: tuple[float, float] | None = None,
    export: Path = MAPS_EXPORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _DEVICE_ID_RE.match(device_id):
        raise SystemExit(f"[ERROR] device_id {device_id!r} must match {_DEVICE_ID_RE.pattern}", 2)
    device = DEVICES[device_id]

    dmap = load_map(device, export)
    bindings = list(dmap["bindings"])
    hints = list(dmap.get("hints", []))
    pad_ch = pad_channels_from_bindings(bindings)

    enriched: list[tuple[int, dict[str, Any], str, str, tuple[str, bool]]] = []
    for idx, b in enumerate(bindings):
        action = b["action"]
        source = b["source"]
        ctype, fb = control_type(action, source, pad_ch)
        sec = section_for(action, ctype)
        enriched.append((idx, b, sec, ctype, (ctype, fb)))

    enriched.sort(
        key=lambda t: (_SECTION_ORDER[t[2]], t[0]),
    )

    sorted_bindings = [t[1] for t in enriched]
    ids = resolve_id_collisions(sorted_bindings)

    type_fallbacks = [ids[i] for i, t in enumerate(enriched) if t[4][1]]

    w_mm, d_mm = panel_size if panel_size else (device.panel_w, device.panel_d)
    rects = placeholder_grid(len(sorted_bindings), w_mm, d_mm)

    controls: list[dict[str, Any]] = []
    ref_controls: list[dict[str, Any]] = []

    for rect, cid, b in zip(rects, ids, sorted_bindings, strict=True):
        x, y, w, h = rect
        action = b["action"]
        source = b["source"]
        ctype, _ = control_type(action, source, pad_ch)
        sec = section_for(action, ctype)
        ctrl = {
            "id": cid,
            "fig": "",
            "type": ctype,
            "label": label_for_action(action),
            "does": does_line(action, device.map_const),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "section": sec,
            "midi": {"ch": source["ch"], "kind": source["kind"], "id": source["id"]},
            "shift_midi": None,
        }
        controls.append(ctrl)
        ref_controls.append({"id": cid, "px": None, "x": x, "y": y, "w": w, "h": h})

    panel_block = {"w": w_mm, "d": d_mm}
    trace_keys = (
        "source",
        "source_pdf_sha256",
        "source_image_sha256",
        "provenance",
        "image_px",
        "panel_px",
        "mm_per_px",
        "method",
        "repeat_spread_mm",
        "issue",
        "date",
    )
    trace = {k: None for k in trace_keys}

    panel_doc: dict[str, Any] = {
        "format": PANEL_FORMAT,
        "device": device_id,
        "display_name": device.display_name,
        "units": "mm",
        "measured": False,
        "panel": panel_block,
        "device_map": device.map_const,
        "sources": {
            "panel_size": device.panel_size_source,
            "wire": f"{device.map_ts} via apps/audio-engine/maps/device-maps.json",
            "positions": ("PLACEHOLDER grid from new_panel.py; not traced (measured: false)"),
        },
        "out_of_scope": [
            "shift_midi: paired at the bind step from the vendor MIDI sheet",
            "map hints are not placed as controls: see scaffold.hints_not_placed",
            "positions are a placeholder grid until the trace step",
        ],
        "scaffold": {
            "generated_by": "tools/deck-diagrams/scripts/new_panel.py",
            "bindings": len(bindings),
            "hints_not_placed": len(hints),
            "type_fallbacks": type_fallbacks,
        },
        "controls": controls,
    }

    ref_doc: dict[str, Any] = {
        "format": PANEL_FORMAT,
        "device": device_id,
        "units": "mm",
        "measured": False,
        "panel": dict(panel_block),
        "trace": trace,
        "controls": ref_controls,
    }
    return panel_doc, ref_doc


def _count_by(keyfn, items: list[dict[str, Any]]) -> Counter:
    return Counter(keyfn(i) for i in items)


def _dry_run_summary(device_id: str, panel: dict[str, Any], stderr_hints: int) -> str:
    ctrls = panel["controls"]
    tcount = _count_by(lambda c: c["type"], ctrls)
    scount = _count_by(lambda c: c["section"], ctrls)
    type_parts = ", ".join(f"{k}={tcount[k]}" for k in sorted(tcount))
    sec_parts = ", ".join(f"{k}={scount[k]}" for k in sorted(scount))
    map_const = panel["device_map"]
    return (
        f"[DRY-RUN] {device_id}: {len(ctrls)} controls from {len(ctrls)} {map_const} bindings; "
        f"types {type_parts}; sections {sec_parts}; {stderr_hints} map hints not placed; "
        "positions measured: false"
    )


def _parse_panel_size(raw: str) -> tuple[float, float]:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)", raw.strip(), re.I)
    if not m:
        raise argparse.ArgumentTypeError("expected WxD in mm, for example 482x272.4")
    w, d = float(m.group(1)), float(m.group(2))
    if w <= 0 or d <= 0:
        raise argparse.ArgumentTypeError("panel size must be positive")
    return w, d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("device", help="device id (e.g. ddj-400)")
    parser.add_argument("--dry-run", action="store_true", help="print JSON to stdout")
    parser.add_argument("--out-root", type=Path, default=DEVICES_DIR)
    parser.add_argument("--panel-size", type=_parse_panel_size, default=None)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else 2

    if args.device not in DEVICES:
        known = ", ".join(sorted(DEVICES))
        print(
            f"[ERROR] unknown device {args.device!r}; add a row to DEVICES in new_panel.py (known: {known})",
            file=sys.stderr,
        )
        return 2

    try:
        panel_size = args.panel_size
        panel, ref = build(args.device, panel_size=panel_size)
        dmap = load_map(DEVICES[args.device], MAPS_EXPORT)
        n_hints = len(dmap.get("hints", []))
    except SystemExit as e:
        if e.args:
            print(str(e.args[0]), file=sys.stderr)
        code = e.code
        if isinstance(code, int):
            return code
        return 2

    if args.dry_run:
        print(json.dumps(panel, indent=1, ensure_ascii=False))
        print(_dry_run_summary(args.device, panel, n_hints), file=sys.stderr)
        return 0

    out_dir = args.out_root / args.device
    out_dir.mkdir(parents=True, exist_ok=True)
    panel_p = out_dir / "panel.json"
    ref_p = out_dir / "panel.reference.json"
    for path in (panel_p, ref_p):
        if path.exists() and not args.force:
            print(
                f"[ERROR] {path} exists; pass --force to overwrite",
                file=sys.stderr,
            )
            return 1

    panel_p.write_text(json.dumps(panel, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    ref_p.write_text(json.dumps(ref, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    n = len(panel["controls"])
    print(f"[OK] wrote {panel_p} and {ref_p} ({n} controls, measured: false)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
