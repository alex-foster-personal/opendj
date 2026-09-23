#!/usr/bin/env python3
"""Generate overlay.html + deck.html from layout.json (offline, no build)."""
from __future__ import annotations

import json
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


def _esc(s: object) -> str:
    """Escape attribute values so labels like 'CALL >' cannot break the tag."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ctrl_style(c: dict) -> str:
    return f"left:{c['x']}%;top:{c['y']}%;width:{c['w']}%;height:{c['h']}%;"


def _ctrl_attrs(c: dict) -> str:
    m = c["midi"]
    shift = m.get("shift_name") or ""
    mode = m.get("mode_name") or ""
    shift_mode = m.get("shift_mode_name") or ""
    return (
        f'data-fig="{_esc(c["fig"])}" data-label="{_esc(c["label"])}" data-kind="{_esc(c["kind"])}" '
        f'data-midi-name="{_esc(m["name"])}" data-midi-type="{_esc(m["type"])}" '
        f'data-midi-ch="{_esc(m["ch"])}" data-midi-code="{_esc(m["code"])}" '
        f'data-shift-name="{_esc(shift)}" data-mode-name="{_esc(mode)}" '
        f'data-shift-mode-name="{_esc(shift_mode)}" data-layer="{_esc(c["layer"])}" '
        f'title="{_esc(c["fig"])} {_esc(c["label"])}"'
    )


def generate(device_dir: Path) -> None:
    layout = json.loads((device_dir / "layout.json").read_text())
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

    js = r"""
const stage = document.getElementById('stage');
const readout = document.getElementById('readout');
const shiftBtn = document.getElementById('shiftBtn');
let shift = false;
const dials = new Map();

function setShift(on) {
  shift = on;
  shiftBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  shiftBtn.classList.toggle('active', on);
  document.querySelectorAll('.control').forEach(el => {
    const has = !!el.dataset.shiftName;
    el.classList.toggle('shift-hot', on && has);
    if (on && has) {
      const base = el.dataset.label;
      el.querySelector('.tag').innerHTML = el.dataset.fig + '<br>SHIFT: ' + el.dataset.shiftName;
      el.dataset._baseLabel = base;
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
      (shift && sn ? ' · <span class="k">SHIFT LAYER ACTIVE</span>' : '');
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
"""

    overlay = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{device} overlay (red recreation)</title>
<style>{CSS_SHARED}</style>
</head>
<body>
<header>
  <h1>{device} - red overlay</h1>
  <span class="meta">Compare recreation vs official plate. Missing = no red box.</span>
  <div class="toolbar">
    <button type="button" id="shiftBtn" aria-pressed="false" title="Toggle SHIFT layer highlight">SHIFT</button>
  </div>
  <span class="legend">Hold keyboard Shift or click SHIFT · hover for MIDI</span>
</header>
<div class="stage-wrap">
  <div class="stage" id="stage">
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
<style>{CSS_SHARED}{DECK_EXTRA}</style>
</head>
<body>
<header>
  <h1>{device} - interactive deck</h1>
  <span class="meta">Usable surface for mapping / teaching. Dials: click to nudge (+Shift = down).</span>
  <div class="toolbar">
    <button type="button" id="shiftBtn" aria-pressed="false" title="Toggle SHIFT layer (exposes shift MIDI twins)">SHIFT</button>
    <a href="overlay.html" style="color:var(--accent);font-size:12px;">open overlay</a>
  </div>
  <span class="legend">Keyboard Shift also toggles layer</span>
</header>
<div class="stage-wrap">
  <div class="stage deck-mode" id="stage">
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
