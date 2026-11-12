"""Part of `build_mixtour_pro.py`, split out with no behavior change: render."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from build_mixtour_pro_model import (
    BILED,
    DEVICE,
    HARDWARE,
    LAYER_TITLE,
    LAYERS,
    PAD_MODES,
    PLATE,
    PLATE_H,
    PLATE_W,
    SOURCES,
    C,
    chan,
    esc,
    hx,
    page_data,
    scope_of,
)
from build_mixtour_pro_page import (
    CSS,
    JS,
    SCOPE_TXT,
    biled_table,
    channel_table,
    control_divs,
    hardware_divs,
    hardware_table,
    pad_mode_table,
    reference_table,
)


def body_html(cs: list[C], plate_src: str) -> str:
    data = json.dumps(page_data(cs), ensure_ascii=False)
    js = JS.replace("__DATA__", data)
    return f"""
<div class="wrap">
<div class="top">
  <div>
    <div class="eyebrow">Reloop, engineered with Laidback Luke, for Algoriddim djay Pro</div>
    <h1>Mixtour Pro control reference</h1>
  </div>
  <div class="sub">Every control on the unit, its BASE, SHIFT, MODE and SHIFT+MODE functions, the MIDI it sends and
  the LED feedback it takes. Numbers are the manual's designation numbers. Hover, tap or tab to a control.</div>
</div>
<div class="bar">
  <span class="eyebrow">Layer</span>
  <div class="seg" role="group" aria-label="Layer">
    <button data-layer="base" aria-pressed="true">Base</button>
    <button data-layer="shift" aria-pressed="false">Shift</button>
    <button data-layer="mode" aria-pressed="false">Mode</button>
    <button data-layer="shift_mode" aria-pressed="false">Shift + Mode</button>
  </div>
  <span class="eyebrow">Decks</span>
  <div class="seg" role="group" aria-label="Deck pair">
    <button data-deck="0" aria-pressed="true">1 / 2</button>
    <button data-deck="1" aria-pressed="false">3 / 4</button>
  </div>
  <span class="hint">Hold Shift or Alt on the keyboard to peek at a layer. Faded outlines have no function on the chosen layer.</span>
</div>
<div class="main">
  <div class="stage-wrap">
    <div class="stage" id="stage">
      <img class="plate" src="{plate_src}" alt="Reloop Mixtour Pro top, rear, front and side panels with designation numbers" width="{PLATE_W}" height="{PLATE_H}"/>
      {control_divs(cs, True)}
      {hardware_divs()}
    </div>
    <div class="legend">
      <span><i style="border-color: var(--base)"></i>Base</span>
      <span><i style="border-color: var(--shift)"></i>Shift</span>
      <span><i style="border-color: var(--mode)"></i>Mode</span>
      <span><i style="border-color: var(--sm)"></i>Shift + Mode</span>
      <span><i style="border-color: var(--hw); border-style: dashed"></i>Hardware only, no MIDI</span>
      <span><i style="border-color: var(--base); border-style: dashed"></i>LED / meter, output only</span>
    </div>
  </div>
  <div id="readout" aria-live="polite">
    <h3>Pick a control</h3>
    <p class="hint">The card shows what the control does on every layer, the exact note or CC it sends for the selected deck pair, and how its LED is driven.</p>
    <p class="hint">Layers on the hardware are real: SHIFT and MODE change the note number the control sends, and each layer has its own LED bank that the firmware shows only while the modifier is held.</p>
  </div>
</div>

<h2>Every control, every layer</h2>
<p class="hint">Channel letters: N = deck transport and mixer, P = deck pads and mode, E = deck FX unit, G = global. Deck d uses N = d, P = 4 + d, E = 8 + d. The scope in square brackets says which deck an address is on: a side control's own deck, the deck whose MODE is held (for MODE layers of a center control), the active decks (crossfader fader start), or the deck the pad bank is assigned to. Sources in brackets, listed at the bottom.</p>
<div class="tw"><table>
<thead><tr><th>#</th><th>Control</th><th>Layer</th><th>Function</th><th>MIDI</th><th>LED feedback</th></tr></thead>
<tbody>{reference_table(cs)}</tbody></table></div>

<h2>Pad modes</h2>
<p>Hold a MODE button and press the pad whose label names the mode. With SPLIT on, the left four pads and the right four pads each hold a mode of their own for their channel [IM]. Pads 1-4 are the left column top to bottom and 5-8 the right column, as captured for the mode-select notes [HW]; the base pad notes are assumed to follow the same order. All eight pad notes exist on every deck's P channel [HW]: a pad's channel is the deck its bank is assigned to, not a property of its column.</p>
<div class="tw"><table>
<thead><tr><th>Select</th><th>Mode</th><th>Pads do</th><th>SHIFT + pad</th><th>Src</th></tr></thead>
<tbody>{pad_mode_table()}</tbody></table></div>

<h2>LED feedback coding</h2>
<div class="cols">
  <div class="card">
    <h3>RGB pads (8)</h3>
    <p>Set by note velocity on the pad's own note. <code>value = 64 * full + 16 * R + 4 * G + B</code>, each of R, G, B from 0 to 3 (0, 33, 66, 100 %). Values 0 to 63 light the dimmed 25 % bank, 64 to 127 the full bank [MM]. The SHIFT pad bank (notes 1C to 23) and the MODE bank (notes 00 to 07) are separate LEDs that only show while that modifier is held [HW].</p>
    <div class="swatches" id="swatches"></div>
    <p class="hint">Swatches show the full-power value in hex; the same color at 25 % is the value minus 64. Hover a swatch for its R/G/B thirds.</p>
  </div>
  <div class="card">
    <h3>Transport bi-LEDs (IN&gt;OUT&gt;EX, SYNC, CUE, PLAY/PAUSE)</h3>
    <div class="tw"><table><thead><tr><th>Value</th><th>Result</th></tr></thead><tbody>{biled_table()}</tbody></table></div>
    <p class="hint">Confirmed by sending raw values with amidi [HW]. Each of the four layers of a button lights a separate bank on its own note; djay uses purple for the MODE overlay.</p>
    <h3>Single-color LEDs</h3>
    <ul>
      <li>NEURAL MIX, HEADPHONE CUE: OFF = 00, DIM = 01, ON = 7F [MM]</li>
      <li>MODE, SPLIT: DIM = 00, ON = 7F (also hardware-lit while held) [MM]</li>
      <li>FX ON paddle LED: OFF = 00, DIM = 01, ON = 02, FLASH = 7F [MM]</li>
      <li>DECK 3 / 4 indicator: N note 08, 00 = decks 1/2, 7F = decks 3/4 [MM]</li>
      <li>VU meter: N CC 1F, value 00 to 06 = segments lit [MM][HW]</li>
      <li>FILTER LED and SHIFT: hardware only, no address [MM]</li>
    </ul>
  </div>
</div>

<h2>MIDI channel key</h2>
<div class="tw"><table><thead><tr><th>Key</th><th>Meaning</th><th>Channels (1-based, hex status low nibble in brackets)</th></tr></thead>
<tbody>{channel_table()}</tbody></table></div>
<p class="hint">SHIFT, MODE and SHIFT+MODE never add a modifier bit: the control sends a different note or CC on each layer, exactly as the table above lists. Button input is OFF = 00 / ON = 7F unless the row says otherwise [MM].</p>

<h2>Hardware without a MIDI address</h2>
<div class="tw"><table><thead><tr><th>#</th><th>Item</th><th>Notes</th></tr></thead><tbody>{hardware_table()}</tbody></table></div>

<h2>Sources and confidence</h2>
<ul>
{"".join(f"<li><b>[{esc(t)}]</b> {esc(r)}</li>" for t, r in SOURCES)}
</ul>
<p>What is inferred rather than documented: the physical order of pads 1-8 for the base pad notes (column-major, from the captured mode-select order); the djay meaning of SHIFT+SPLIT, SHIFT+MODE, SHIFT+PARAM, SHIFT+BROWSE turn and MODE+DRY/WET, which have addresses in the manufacturer map but no function in the manual or on the label. Everything else is quoted from the manual, the silkscreen, Reloop's signature-features sheet, the manufacturer MIDI map, or a hardware capture.</p>
<p class="hint">Regenerate with <code>uv run python (private path)</code>.</p>
</div>
<script>{js}</script>
"""


def deck_html(cs: list[C]) -> str:
    fonts = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700'
        '&family=Barlow:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap">'
    )
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>\n'
        f"<title>{DEVICE} control reference</title>\n{fonts}\n<style>{CSS}</style>\n</head>\n<body>"
        + body_html(cs, PLATE)
        + "\n</body>\n</html>\n"
    )


def artifact_html(cs: list[C], plate_png: Path) -> str:
    """Skeleton-free variant with the plate inlined, for publishing as a claude.ai artifact."""
    b64 = base64.b64encode(plate_png.read_bytes()).decode("ascii")
    fonts = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700'
        '&family=Barlow:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap">'
    )
    return f"<title>Mixtour Pro Control Reference</title>\n{fonts}\n<style>{CSS}</style>\n" + body_html(
        cs, f"data:image/png;base64,{b64}"
    )


def overlay_html(cs: list[C]) -> str:
    css = (
        CSS
        + "\n.control, .hw { border-color: #ff2a2a !important; color: #ff2a2a !important; background: rgba(255,40,40,.12) !important; }\n"
    )
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f'<title>{DEVICE} overlay (red recreation)</title>\n<style>{css}</style>\n</head>\n<body>\n<div class="wrap">'
        f'<div class="top"><h1>{DEVICE} overlay</h1><div class="sub">Red hit targets over the manual\'s designation '
        "figure. Used to diff geometry against the plate; see deck.html for the usable reference.</div></div>"
        f'<div class="stage" id="stage"><img class="plate" src="{PLATE}" alt="plate"/>{control_divs(cs, True)}{hardware_divs()}</div>'
        '<div id="readout" class="hint" style="margin-top:12px">hover a target</div></div>\n'
        "<script>document.querySelectorAll('.control,.hw').forEach(el=>el.addEventListener('pointerenter',()=>{"
        "document.getElementById('readout').textContent=el.title+(el.dataset.midiName?' | '+el.dataset.midiType+' ch'+el.dataset.midiCh+' code '+el.dataset.midiCode:'');}));</script>\n"
        "</body>\n</html>\n"
    )


def reference_md(cs: list[C]) -> str:
    lines = [
        f"# {DEVICE}: exhaustive control reference",
        "",
        "Generated by `(private path)`; do not edit by hand.",
        "Interactive version: `deck.html` (same directory). Channel key: N deck transport+mixer,",
        "P deck pads/mode, E deck FX unit, G global; deck 1 shown, deck d adds d-1 to N/P/E.",
        "",
        "| # | Control | Layer | Function | MIDI (deck 1) | LED feedback |",
        "|---|---|---|---|---|---|",
    ]
    for c in cs:
        first = True
        for ly in LAYERS:
            f = c.layers.get(ly)
            if not f:
                continue
            m = f.midi
            midi = (
                "hardware only"
                if not m
                else (
                    f"{'OUT ' if m.direction == 'out' else ''}{m.type} ch {chan(m.letter)} ({m.letter}) 0x{hx(m.code)}"
                    + (f" LED 0x{hx(m.led)}" if m.led is not None else "")
                    + f" [{SCOPE_TXT[scope_of(c, m)]}]"
                    + (f"; in {m.values_in}" if m.values_in else "")
                    + (f"; out {m.values_out}" if m.values_out else "")
                )
            )
            led = c.led.replace("|", "/") if first else ""
            lines.append(
                f"| {c.num if first else ''} | {('**' + c.label + '**') if first else ''} | {LAYER_TITLE[ly]} | "
                f"{f.text.replace('|', '/')} [{f.src}] | {midi} | {led} |"
            )
            first = False
    lines += ["", "## Pad modes", "", "| Select | Mode | Pads do | SHIFT + pad |", "|---|---|---|---|"]
    for pad, mode, how, shift, src in PAD_MODES:
        lines.append(f"| MODE + pad {pad} | {mode} | {how} | {shift} [{src}] |")
    lines += [
        "",
        "## LED coding",
        "",
        "- RGB pads: value = 64*full + 16*R + 4*G + B, R/G/B in 0..3 (0/33/66/100%); 0..63 dimmed 25%, 64..127 full [MM].",
        "- Transport bi-LEDs: " + "; ".join(f"0x{hx(v)} {t}" for v, t in BILED) + " [MM][HW].",
        "- NEURAL MIX / HEADPHONE CUE: OFF 00, DIM 01, ON 7F. MODE / SPLIT: DIM 00, ON 7F. FX ON: OFF 00, DIM 01, ON 02, FLASH 7F. "
        "DECK 3/4 LED: N note 08, 00 / 7F. VU: N CC 1F 00..06 [MM].",
        "",
        "## Hardware without MIDI",
        "",
    ]
    for n, l, _b, t in HARDWARE:
        lines.append(f"- {n} {l}: {t}")
    lines += ["", "## Sources", ""] + [f"- [{t}] {r}" for t, r in SOURCES] + [""]
    return "\n".join(lines)
