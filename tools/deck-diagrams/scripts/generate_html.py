#!/usr/bin/env python3
"""Generate overlay.html + deck.html from layout.json (offline, no build)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CSS_SHARED = """
:root {
  --bg: #0e1114;
  --ink: #e8eef2;
  --muted: #8a9aa6;
  --red: #ff2a2a;
  --accent: #3dd6c6;
  --panel: #1a2229;
  --shift: #ffb020;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
  background: var(--bg); color: var(--ink);
}
header {
  display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
  padding: 10px 14px; background: var(--panel); border-bottom: 1px solid #2a3540;
}
header h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: 0.02em; }
header .meta { color: var(--muted); font-size: 12px; }
.toolbar button, .toolbar label {
  background: #24303a; color: var(--ink); border: 1px solid #3a4a57;
  border-radius: 4px; padding: 6px 10px; font-size: 12px; cursor: pointer;
}
.toolbar button.active, .toolbar button[aria-pressed="true"] {
  background: var(--shift); color: #111; border-color: var(--shift); font-weight: 600;
}
.stage-wrap { padding: 12px; overflow: auto; }
.stage {
  position: relative; width: min(1200px, 100%);
  margin: 0 auto; user-select: none;
}
.stage img.plate { width: 100%; height: auto; display: block; border-radius: 4px; }
.stage img.plate[hidden] { display: none !important; }
.control {
  position: absolute; border: 2px solid var(--red); color: var(--red);
  background: rgba(255, 40, 40, 0.12); font-size: 9px; line-height: 1.1;
  display: flex; align-items: flex-start; justify-content: flex-start;
  padding: 1px 2px; overflow: hidden; cursor: pointer;
}
.control .tag { font-weight: 700; pointer-events: none; }
.control.kind-jog { border-radius: 50%; }
.control.kind-knob, .control.kind-encoder { border-radius: 50%; }
.control.kind-fader { border-radius: 2px; }
.control.kind-pad { border-radius: 3px; }
.control.pressed, .control:active { background: rgba(255,40,40,0.45); }
.control.shift-hot { border-color: var(--shift); color: var(--shift); background: rgba(255,176,32,0.18); }
#readout {
  position: sticky; bottom: 0; background: #12181e; border-top: 1px solid #2a3540;
  padding: 10px 14px; font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 12px; min-height: 52px;
}
#readout .k { color: var(--accent); }
.legend { color: var(--muted); font-size: 11px; margin-left: auto; }
.pad-modes { color: var(--ink); font-size: 12px; display: inline-flex; gap: 6px; align-items: center; }
.pad-modes select {
  background: #24303a; color: var(--ink); border: 1px solid #3a4a57;
  border-radius: 4px; padding: 5px 8px; font-size: 12px;
}
.plate-missing {
  margin: 0 auto 12px; max-width: 1200px; padding: 10px 14px;
  background: #3a2410; border: 1px solid var(--shift); color: var(--shift);
  border-radius: 4px; font-size: 13px;
}
"""

DECK_EXTRA = """
.stage.deck-mode img.plate { opacity: 0.35; filter: grayscale(0.3); }
.stage.deck-mode .control {
  border-color: #6af; color: #cfe; background: rgba(40,80,120,0.55);
  align-items: center; justify-content: center; text-align: center;
  font-size: 10px; font-weight: 600;
}
.stage.deck-mode .control.shift-hot {
  border-color: var(--shift); color: #211; background: rgba(255,176,32,0.75);
}
.stage.deck-mode .control.kind-knob::after,
.stage.deck-mode .control.kind-encoder::after {
  content: ""; position: absolute; width: 40%; height: 40%;
  border: 2px solid currentColor; border-radius: 50%; top: 30%; left: 30%;
  pointer-events: none;
}
.dial-value { position: absolute; bottom: 2px; right: 3px; font-size: 8px; opacity: 0.85; }
"""


PAD_ROW = re.compile(r"^pad(\d)_(.+)$")
DECKS = 4  # a pad row on channel c also serves the next DECKS-1 channels (one per deck)


def _modes_for(rows: list[dict], pad: int, ch: int) -> list[dict]:
    """Mode rows for one pad, in midi.json order, read at generate time (nothing is stored in layout.json).

    midi.json keeps one representative channel per pad family (xdj-az: pad 1-4 on ch 6, pad 5-8 on ch 10;
    ch 7-9 and 11-13 are decks 2-4). A control on `ch` resolves to the pad's row channel that is at most
    DECKS-1 below it; the control keeps its own channel for display.
    """
    pad_rows = [r for r in rows if r["type"] == "note" and (m := PAD_ROW.match(r["name"])) and int(m.group(1)) == pad]
    deck_chs = {r["ch"] for r in pad_rows if 0 <= ch - r["ch"] < DECKS}
    if not deck_chs:
        return []
    include_chs = set(deck_chs)
    base_ch = max(deck_chs)
    # FLX10/DDJ-1000: shift twins on ch+1; XDJ-AZ keeps base and shift on the same ch.
    for r in pad_rows:
        if r["ch"] != base_ch + 1:
            continue
        stem = PAD_ROW.match(r["name"]).group(2)
        if not stem.endswith("_shift"):
            continue
        base_name = f"pad{pad}_{stem[:-6]}"
        if any(x["name"] == base_name and x["ch"] == base_ch for x in pad_rows):
            include_chs.add(base_ch + 1)
            break
    by_name = {r["name"]: r for r in pad_rows if r["ch"] in include_chs}
    out: dict[str, dict] = {}
    for name, r in by_name.items():
        stem = PAD_ROW.match(name).group(2)
        if r.get("layer") == "shift" and stem.endswith("_shift") and f"pad{pad}_{stem[:-6]}" in by_name:
            continue  # shift twin of a base row, attached below
        if r.get("layer") == "shift":
            mode = stem[:-6] if stem.endswith("_shift") else stem
            out[mode] = {"mode": mode, "name": None, "code": None, "shift_name": name, "shift_code": r["code"]}
            continue
        entry = {"mode": stem, "name": name, "code": r["code"], "shift_name": None, "shift_code": None}
        twin = by_name.get(f"{name}_shift")
        if twin is not None and twin.get("layer", "shift") != "base":
            entry["shift_name"], entry["shift_code"] = twin["name"], twin["code"]
        out[stem] = entry
    return list(out.values())


def _join_pad_modes(layout: dict, device_dir: Path) -> None:
    """Attach midi.pad_modes (in memory only) to every kind=pad control that midi.json has mode rows for."""
    midi_path = device_dir / "midi.json"
    if not midi_path.exists():
        return
    rows = json.loads(midi_path.read_text())["controls"]
    for c in layout["controls"]:
        m = c.get("midi")
        pm = PAD_ROW.match(m["name"] or "") if c["kind"] == "pad" and m else None
        if pm:
            modes = _modes_for(rows, int(pm.group(1)), m["ch"])
            if modes:
                m["pad_modes"] = modes


def _esc(s: object) -> str:
    """Escape attribute values so labels like 'CALL >' cannot break the tag."""
    return str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _ctrl_style(c: dict) -> str:
    return f"left:{c['x']}%;top:{c['y']}%;width:{c['w']}%;height:{c['h']}%;"


def _ctrl_attrs(c: dict) -> str:
    m = c["midi"]
    shift = m.get("shift_name") or ""
    mode = m.get("mode_name") or ""
    shift_mode = m.get("shift_mode_name") or ""
    pm = m.get("pad_modes")
    pad_modes = f' data-pad-modes="{_esc(json.dumps(pm, separators=(",", ":")))}"' if pm else ""
    return (
        f'data-fig="{_esc(c["fig"])}" data-label="{_esc(c["label"])}" data-kind="{_esc(c["kind"])}" '
        f'data-midi-name="{_esc(m["name"])}" data-midi-type="{_esc(m["type"])}" '
        f'data-midi-ch="{_esc(m["ch"])}" data-midi-code="{_esc(m["code"])}" '
        f'data-shift-name="{_esc(shift)}" data-mode-name="{_esc(mode)}" '
        f'data-shift-mode-name="{_esc(shift_mode)}" data-layer="{_esc(c["layer"])}" '
        f'title="{_esc(c["fig"])} {_esc(c["label"])}"{pad_modes}'
    )


def _stage_style(plate: dict) -> str:
    """Reserve the plate's aspect ratio so percentage-placed controls keep their geometry without the image."""
    w, h = plate.get("width"), plate.get("height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        return f' style="aspect-ratio:{w:g}/{h:g};"'
    return ""


def _risk_css(layout: dict) -> str:
    rb = layout.get("risk_banner")
    if not rb:
        return ""
    return (
        ".risk-banner{background:#5a4300;color:#ffe08a;border-bottom:2px solid #ffcc00;"
        "padding:8px 14px;font-size:13px;font-weight:600;}"
        f'.control[data-fig="{rb["fig"]}"]{{outline:3px dashed #ffcc00;outline-offset:1px;}}'
    )


def _risk_html(layout: dict) -> str:
    rb = layout.get("risk_banner")
    if not rb:
        return ""
    return f'<div class="risk-banner" role="alert">{_esc(rb["text"])}</div>\n'


def _pad_modes_toolbar(layout: dict) -> str:
    """A PAD MODE selector when any pad control carries midi.pad_modes (joined from midi.json at generate time)."""
    modes: list[str] = []
    for c in layout["controls"]:
        for m in c["midi"].get("pad_modes") or []:
            if m["mode"] not in modes:
                modes.append(m["mode"])
    if not modes:
        return ""
    opts = "".join(f'<option value="{_esc(m)}">{_esc(m.replace("_", " ").upper())}</option>' for m in modes)
    return f'<label class="pad-modes">PAD MODE <select id="padMode">{opts}</select></label>'


def generate(device_dir: Path) -> None:
    layout = json.loads((device_dir / "layout.json").read_text())
    _join_pad_modes(layout, device_dir)
    plate = layout["plate"]["image"]
    device = layout["device"]

    overlay_ctrls = []
    deck_ctrls = []
    for c in layout["controls"]:
        common = (
            f'<div class="control kind-{c["kind"]}" style="{_ctrl_style(c)}" {_ctrl_attrs(c)}>'
            f'<span class="tag">{c["fig"]}</span></div>'
        )
        overlay_ctrls.append(common)
        label = c["label"]
        deck_ctrls.append(
            f'<div class="control kind-{c["kind"]}" style="{_ctrl_style(c)}" {_ctrl_attrs(c)}>'
            f'<span class="tag">{c["fig"]}<br>{label}</span>'
            f'<span class="dial-value" hidden>64</span></div>'
        )

    risk_css, risk_html = _risk_css(layout), _risk_html(layout)
    stage_style, pad_toolbar = _stage_style(layout["plate"]), _pad_modes_toolbar(layout)

    js = r"""
const stage = document.getElementById('stage');
const readout = document.getElementById('readout');
const shiftBtn = document.getElementById('shiftBtn');
let shift = false;
const dials = new Map();

const padModeSel = document.getElementById('padMode');

function padModeReadout(el) {
  if (!padModeSel || !el.dataset.padModes) return '';
  const mode = JSON.parse(el.dataset.padModes).find(m => m.mode === padModeSel.value);
  if (!mode) return ' · pad mode <span class="k">' + padModeSel.value + '</span>: no wire in midi.json';
  const note = (n, c) => n ? `<span class="k">${n}</span> Note ${c} ch${el.dataset.midiCh}` : 'none';
  return ` · pad mode <span class="k">${mode.mode}</span>: ${note(mode.name, mode.code)}` +
    ` · shift twin ${note(mode.shift_name, mode.shift_code)}`;
}

function setShift(on) {
  shift = on;
  shiftBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  shiftBtn.classList.toggle('active', on);
  document.querySelectorAll('.control').forEach(el => {
    const has = !!el.dataset.shiftName;
    el.classList.toggle('shift-hot', on && has);
    if (on && has) {
      el.querySelector('.tag').innerHTML = el.dataset.fig + '<br>SHIFT: ' + el.dataset.shiftName;
    } else if (el.dataset._baseHtml) {
      el.querySelector('.tag').innerHTML = el.dataset._baseHtml;
    }
  });
}

document.querySelectorAll('.control').forEach(el => {
  el.dataset._baseHtml = el.querySelector('.tag').innerHTML;
  el.addEventListener('pointerenter', () => {
    const sn = el.dataset.shiftName;
    readout.innerHTML =
      `<span class="k">${el.dataset.fig}</span> ${el.dataset.label} · ` +
      `<span class="k">${el.dataset.midiType.toUpperCase()}</span> ch${el.dataset.midiCh} ` +
      `code ${el.dataset.midiCode} · name <span class="k">${el.dataset.midiName}</span>` +
      (sn ? ` · shift twin <span class="k">${sn}</span>` : ' · (no shift twin)') +
      (el.dataset.modeName ? ` · mode twin <span class="k">${el.dataset.modeName}</span>` : '') +
      (el.dataset.shiftModeName ? ` · shift+mode twin <span class="k">${el.dataset.shiftModeName}</span>` : '') +
      (shift && sn ? ' · <span class="k">SHIFT LAYER ACTIVE</span>' : '') +
      padModeReadout(el);
  });
  el.addEventListener('pointerdown', (ev) => {
    el.classList.add('pressed');
    if (el.dataset.kind === 'knob' || el.dataset.kind === 'encoder' || el.dataset.kind === 'fader') {
      const id = el.dataset.fig;
      let v = dials.get(id) ?? 64;
      v = Math.max(0, Math.min(127, v + (ev.shiftKey ? -8 : 8)));
      dials.set(id, v);
      const dv = el.querySelector('.dial-value');
      if (dv) { dv.hidden = false; dv.textContent = String(v); }
      readout.innerHTML += ` · value <span class="k">${v}</span>`;
    }
  });
  el.addEventListener('pointerup', () => el.classList.remove('pressed'));
  el.addEventListener('pointerleave', () => el.classList.remove('pressed'));
});

shiftBtn?.addEventListener('click', () => setShift(!shift));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Shift') setShift(true);
});
document.addEventListener('keyup', (e) => {
  if (e.key === 'Shift') setShift(false);
});

document.querySelectorAll('img.plate').forEach(img => {
  const markMissing = () => {
    if (img.dataset.missingShown === '1') return;
    img.dataset.missingShown = '1';
    img.hidden = true;
    const note = document.createElement('div');
    note.className = 'plate-missing';
    note.setAttribute('role', 'alert');
    note.textContent = 'Plate image missing: ' + (img.getAttribute('src') || '') +
      ' is not in this checkout. Control boxes still work.';
    const host = img.closest('.stage');
    (host || img).insertAdjacentElement('beforebegin', note);
  };
  img.addEventListener('error', markMissing);
  if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) markMissing();
});
"""

    overlay = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{device} overlay (red recreation)</title>
<style>{CSS_SHARED}{risk_css}</style>
</head>
<body>
<header>
  <h1>{device} - red overlay</h1>
  <span class="meta">Compare recreation vs official plate. Missing = no red box.</span>
  <div class="toolbar">
    <button type="button" id="shiftBtn" aria-pressed="false" title="Toggle SHIFT layer highlight">SHIFT</button>
    {pad_toolbar}
  </div>
  <span class="legend">Hold keyboard Shift or click SHIFT · hover for MIDI</span>
</header>
{risk_html}<div class="stage-wrap">
  <div class="stage" id="stage"{stage_style}>
    <img class="plate" src="{plate}" alt="{device} official plate"/>
    {"".join(overlay_ctrls)}
  </div>
</div>
<div id="readout">Hover a control…</div>
<script>{js}</script>
</body>
</html>
"""

    deck = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{device} interactive deck</title>
<style>{CSS_SHARED}{DECK_EXTRA}{risk_css}</style>
</head>
<body>
<header>
  <h1>{device} - interactive deck</h1>
  <span class="meta">Usable surface for mapping / teaching. Dials: click to nudge (+Shift = down).</span>
  <div class="toolbar">
    <button type="button" id="shiftBtn" aria-pressed="false" title="Toggle SHIFT layer (exposes shift MIDI twins)">SHIFT</button>
    {pad_toolbar}
    <a href="overlay.html" style="color:var(--accent);font-size:12px;">open overlay</a>
  </div>
  <span class="legend">Keyboard Shift also toggles layer</span>
</header>
{risk_html}<div class="stage-wrap">
  <div class="stage deck-mode" id="stage"{stage_style}>
    <img class="plate" src="{plate}" alt="{device} plate underlay"/>
    {"".join(deck_ctrls)}
  </div>
</div>
<div id="readout">Hover a control…</div>
<script>{js}</script>
</body>
</html>
"""

    (device_dir / "overlay.html").write_text(overlay)
    (device_dir / "deck.html").write_text(deck)
    print(f"wrote overlay.html + deck.html ({len(layout['controls'])} controls)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: generate_html.py <device_dir>", file=sys.stderr)
        sys.exit(2)
    generate(Path(sys.argv[1]))
