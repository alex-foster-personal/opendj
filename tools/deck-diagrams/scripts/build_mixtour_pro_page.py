"""Part of `build_mixtour_pro.py`, split out with no behavior change: page."""

from __future__ import annotations

from build_mixtour_pro_model import (
    BILED,
    CHANNEL_KEY,
    HARDWARE,
    LAYER_TITLE,
    LAYERS,
    PAD_MODES,
    C,
    chan,
    esc,
    hx,
    pct,
    placements,
    scope_of,
    side_deck,
)

CSS = r"""
:root {
  --bg: #f4f5f2; --ink: #171a1d; --muted: #5b6670; --panel: #ffffff; --line: #d6dbd8;
  --plate-ground: #ffffff;
  --base: #2f5d84; --shift: #e07f1a; --mode: #6d4fd1; --sm: #b8306e;
  --hw: #7d8890; --hit: rgba(47,93,132,.10); --accent: #2f5d84;
  --led-red: #ff3b3b; --led-blue: #2f6bff; --led-purple: #b04cff;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: "Barlow", "Helvetica Neue", Arial, sans-serif;
  --disp: "Barlow Condensed", "Arial Narrow", Arial, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #101316; --ink: #e9edf0; --muted: #98a4ad; --panel: #181d22; --line: #2a3238;
    --base: #6ea7d8; --shift: #f39a3a; --mode: #a78bfa; --sm: #f06aa6; --hw: #6b7780;
    --hit: rgba(110,167,216,.14); --accent: #6ea7d8;
  }
}
:root[data-theme="dark"] {
  --bg: #101316; --ink: #e9edf0; --muted: #98a4ad; --panel: #181d22; --line: #2a3238;
  --base: #6ea7d8; --shift: #f39a3a; --mode: #a78bfa; --sm: #f06aa6; --hw: #6b7780;
  --hit: rgba(110,167,216,.14); --accent: #6ea7d8;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 15px; line-height: 1.45;
  padding-inline: 16px; padding-block: 0 48px; }
h1, h2, h3 { font-family: var(--disp); text-wrap: balance; letter-spacing: .01em; margin: 0; }
h1 { font-size: clamp(28px, 5vw, 44px); font-weight: 700; text-transform: uppercase; line-height: 1; }
h2 { font-size: 26px; font-weight: 600; text-transform: uppercase; margin-block: 40px 12px; padding-top: 12px;
  border-top: 2px solid var(--ink); }
h3 { font-size: 18px; font-weight: 600; margin-block: 18px 6px; }
p { max-width: 70ch; }
code, .mono { font-family: var(--mono); font-size: .92em; }
.wrap { max-width: 1280px; margin: 0 auto; }
.top { display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: end; padding-block: 22px 12px; }
.top .sub { color: var(--muted); font-size: 14px; max-width: 60ch; }
.eyebrow { font-family: var(--disp); text-transform: uppercase; letter-spacing: .12em; font-size: 12px; color: var(--muted); }
.bar { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg);
  border-block: 1px solid var(--line); padding-block: 8px; display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg button { font: 600 13px/1 var(--disp); text-transform: uppercase; letter-spacing: .06em; padding: 9px 12px;
  background: var(--panel); color: var(--ink); border: 0; border-right: 1px solid var(--line); cursor: pointer; }
.seg button:last-child { border-right: 0; }
.seg button[aria-pressed="true"] { color: #fff; }
.seg button[data-layer="base"][aria-pressed="true"] { background: var(--base); }
.seg button[data-layer="shift"][aria-pressed="true"] { background: var(--shift); }
.seg button[data-layer="mode"][aria-pressed="true"] { background: var(--mode); }
.seg button[data-layer="shift_mode"][aria-pressed="true"] { background: var(--sm); }
.seg button[data-deck][aria-pressed="true"] { background: var(--ink); color: var(--bg); }
.seg button:focus-visible, .control:focus-visible, .hw:focus-visible { outline: 3px solid var(--shift); outline-offset: 1px; }
.hint { color: var(--muted); font-size: 13px; }
.main { display: grid; grid-template-columns: minmax(0, 1fr) minmax(300px, 380px); gap: 20px; align-items: start; margin-top: 16px; }
@media (max-width: 900px) { .main { grid-template-columns: 1fr; } }
.stage-wrap { overflow: auto; }
.stage { position: relative; width: 100%; max-width: 760px; margin: 0 auto; user-select: none; background: var(--plate-ground); border-radius: 6px; }
.stage img.plate { width: 100%; height: auto; display: block; border-radius: 6px; }
.control, .hw { position: absolute; display: flex; align-items: center; justify-content: center; text-align: center;
  padding: 0; overflow: hidden; cursor: pointer; border: 2px solid var(--base); background: var(--hit);
  color: var(--base); font: 700 clamp(7px, 1.15vw, 10px)/1.05 var(--disp); text-transform: uppercase; letter-spacing: .02em; }
.control .tag { pointer-events: none; padding: 1px; }
.control.kind-knob, .control.kind-encoder { border-radius: 50%; }
.control.kind-pad { border-radius: 3px; }
.control.kind-fader { border-radius: 3px; }
.control.kind-meter, .control.kind-other { border-style: dashed; opacity: .85; }
.hw { border: 2px dashed var(--hw); color: var(--hw); background: transparent; }
.control.has-layer { border-color: var(--layer-color); color: var(--layer-color); background: color-mix(in srgb, var(--layer-color) 14%, transparent); }
.control.no-layer { opacity: .28; }
.control.active, .hw.active { box-shadow: 0 0 0 3px var(--bg), 0 0 0 5px currentColor; z-index: 2; }
.control.pressed { background: color-mix(in srgb, currentColor 40%, transparent); }
.num { position: absolute; top: -1px; left: -1px; font: 700 8px/1 var(--mono); background: var(--ink); color: var(--bg);
  padding: 1px 3px; border-radius: 0 0 3px 0; pointer-events: none; }
#readout { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 56px); background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 14px 16px; min-height: 120px; font-size: 14px; }
@media (max-width: 900px) { #readout { position: static; } }
#readout .k { font-family: var(--mono); color: var(--accent); }
#readout h3 { margin-top: 0; font-size: 22px; }
#readout .desig { display: inline-block; font: 700 12px/1 var(--mono); background: var(--ink); color: var(--bg); padding: 3px 6px; border-radius: 3px; margin-right: 6px; vertical-align: middle; }
.lrow { display: grid; grid-template-columns: 92px 1fr; gap: 6px 10px; padding: 8px 0; border-top: 1px solid var(--line); }
.lrow .ltag { font: 700 11px/1.2 var(--disp); text-transform: uppercase; letter-spacing: .08em; padding: 3px 6px; border-radius: 3px; color: #fff; height: fit-content; }
.lrow[data-layer="base"] .ltag { background: var(--base); }
.lrow[data-layer="shift"] .ltag { background: var(--shift); }
.lrow[data-layer="mode"] .ltag { background: var(--mode); }
.lrow[data-layer="shift_mode"] .ltag { background: var(--sm); }
.lrow .midi { font-family: var(--mono); font-size: 12px; color: var(--muted); margin-top: 3px; }
.ledbox { border-top: 1px solid var(--line); padding-top: 8px; margin-top: 6px; color: var(--muted); font-size: 13px; }
.ledbox b { color: var(--ink); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; color: var(--muted); margin-top: 10px; }
.legend i { display: inline-block; width: 12px; height: 12px; border: 2px solid; margin-right: 4px; vertical-align: -2px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
.tw { overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }
th, td { text-align: left; vertical-align: top; padding: 7px 9px; border-bottom: 1px solid var(--line); }
th { font-family: var(--disp); text-transform: uppercase; letter-spacing: .06em; font-size: 12px; background: var(--panel); position: sticky; top: 0; }
tr.sec td { background: var(--panel); font-family: var(--disp); text-transform: uppercase; letter-spacing: .06em; font-weight: 700; }
td.n { font-family: var(--mono); white-space: nowrap; }
td.l { white-space: nowrap; }
.pill { display: inline-block; font: 700 10px/1.2 var(--disp); text-transform: uppercase; letter-spacing: .08em; padding: 3px 6px; border-radius: 3px; color: #fff; }
.pill.base { background: var(--base); } .pill.shift { background: var(--shift); } .pill.mode { background: var(--mode); } .pill.shift_mode { background: var(--sm); }
.src { color: var(--muted); font-family: var(--mono); font-size: 11px; }
.swatches { display: grid; grid-template-columns: repeat(16, 1fr); gap: 3px; max-width: 760px; }
.sw { aspect-ratio: 1; border-radius: 3px; border: 1px solid var(--line); position: relative; }
.sw span { position: absolute; inset: auto 0 0 0; font: 600 8px/1 var(--mono); text-align: center; color: #fff; text-shadow: 0 0 2px #000; padding-bottom: 1px; }
.led { display: inline-block; width: 14px; height: 14px; border-radius: 50%; vertical-align: -3px; margin-right: 6px; border: 1px solid var(--line); }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; }
ul { padding-left: 20px; } li { margin-bottom: 4px; }
@media (prefers-reduced-motion: no-preference) { .control, .hw { transition: box-shadow .12s, background .12s; } }
"""

JS = r"""
const DATA = __DATA__;
const stage = document.getElementById('stage');
const readout = document.getElementById('readout');
const byFig = Object.fromEntries(DATA.controls.map(c => [c.fig, c]));
let layer = 'base';
let deckPair = 0; // 0 = decks 1/2, 1 = decks 3/4
let activeEl = null;
const LCOLOR = { base: 'var(--base)', shift: 'var(--shift)', mode: 'var(--mode)', shift_mode: 'var(--sm)' };
const LSHORT = { base: '', shift: 'SHIFT', mode: 'MODE', shift_mode: 'SHIFT+MODE' };

function chNum(letter, deck) {
  if (letter === 'G') return 16;
  return { N: 0, P: 4, E: 8 }[letter] + deck;
}
function leftDeck() { return 1 + deckPair * 2; }
function rightDeck() { return 2 + deckPair * 2; }
function deckFor(el) {
  // The deck of the side this hit target sits on (null for a center control).
  if (el.dataset.side === 'r') return rightDeck();
  if (el.dataset.side === 'l') return leftDeck();
  return null;
}
function chTxt(letter, deck) {
  const ch = chNum(letter, deck);
  return `ch ${ch} (${letter} ${(ch - 1).toString(16).toUpperCase().padStart(2, '0')})`;
}
function channelText(m, el) {
  // Per-layer scope decides which deck's channel the address is on.
  const L = leftDeck(), R = rightDeck();
  switch (m.scope) {
    case 'global': return chTxt('G', 16);
    case 'side': { const d = deckFor(el) || L; return `deck ${d}: ${chTxt(m.letter, d)}`; }
    case 'modifier_side': return `deck of the side whose MODE is held: left MODE = deck ${L} ${chTxt(m.letter, L)}, right MODE = deck ${R} ${chTxt(m.letter, R)}`;
    case 'active_decks': return `follows the active decks: left = deck ${L} ${chTxt(m.letter, L)}, right = deck ${R} ${chTxt(m.letter, R)}`;
    case 'pad_bank': return `deck the pad bank is assigned to: deck ${L} ${chTxt(m.letter, L)} or deck ${R} ${chTxt(m.letter, R)}`;
    default: return chTxt(m.letter, 1);
  }
}
function midiText(m, el) {
  if (!m) return 'no MIDI on this layer';
  const t = m.type.toUpperCase();
  const dir = m.dir === 'out' ? 'OUT ' : '';
  let s = `${dir}${t} ${m.type === 'note' ? 'note' : 'cc'} 0x${m.hex} (${m.code}) on ${channelText(m, el)}`;
  if (m.in) s += ` | in: ${m.in}`;
  if (m.led) s += ` | LED note 0x${m.led}`;
  if (m.out) s += ` | out: ${m.out}`;
  return s;
}
function shortLabel(c, ly) {
  const f = c.layers[ly];
  return f ? f.short : c.label;
}
function applyLayer() {
  document.querySelectorAll('.seg button[data-layer]').forEach(b => b.setAttribute('aria-pressed', b.dataset.layer === layer ? 'true' : 'false'));
  document.querySelectorAll('.control').forEach(el => {
    const c = byFig[el.dataset.fig];
    const has = !!c.layers[layer];
    el.classList.toggle('has-layer', has);
    el.classList.toggle('no-layer', !has);
    el.style.setProperty('--layer-color', LCOLOR[layer]);
    const tag = el.querySelector('.tag');
    tag.textContent = has ? (layer === 'base' ? (c.padMode ? `PAD ${c.fig.split('_')[1]} / ${c.padMode}` : c.label) : shortLabel(c, layer)) : c.label;
  });
  if (activeEl) show(activeEl);
}
function applyDeck() {
  document.querySelectorAll('.seg button[data-deck]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.deck) === String(deckPair) ? 'true' : 'false'));
  if (activeEl) show(activeEl);
}
function show(el) {
  document.querySelectorAll('.active').forEach(a => a.classList.remove('active'));
  el.classList.add('active');
  activeEl = el;
  if (el.classList.contains('hw')) {
    const h = DATA.hardware[+el.dataset.hw];
    readout.innerHTML = `<h3><span class="desig">${esc(h.num)}</span>${esc(h.label)}</h3>` +
      `<p>${esc(h.text)}</p><div class="ledbox">No MIDI address. Hardware only.</div>`;
    return;
  }
  const c = byFig[el.dataset.fig];
  const deck = deckFor(el);
  const baseScope = c.layers.base.midi ? c.layers.base.midi.scope : 'global';
  const sideTxt = baseScope === 'side' ? ` <span class="hint">deck ${deck} (${el.dataset.side === 'r' ? 'right' : 'left'} side, decks ${deckPair ? '3/4' : '1/2'})</span>`
    : baseScope === 'pad_bank' ? ` <span class="hint">${el.dataset.side === 'r' ? 'right' : 'left'} column; channel follows the pad bank's deck</span>`
    : ' <span class="hint">global control; layers on a deck channel say which deck below</span>';
  let h = `<h3><span class="desig">${esc(c.num)}</span>${esc(c.label)}</h3>` +
    `<div class="hint">${esc(c.kind)} in ${esc(c.section)} section${sideTxt}</div>`;
  for (const ly of DATA.layers) {
    const f = c.layers[ly];
    if (!f) continue;
    h += `<div class="lrow" data-layer="${ly}"${ly === layer ? ' style="background: color-mix(in srgb, currentColor 6%, transparent)"' : ''}>` +
      `<span class="ltag">${esc(DATA.layerTitle[ly])}</span><div>${esc(f.text)}<div class="midi">${esc(midiText(f.midi, el))}</div></div></div>`;
  }
  h += `<div class="ledbox"><b>LED feedback.</b> ${esc(c.led)}</div>`;
  if (c.notes) h += `<div class="ledbox">${esc(c.notes)}</div>`;
  readout.innerHTML = h;
}
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

document.querySelectorAll('.control, .hw').forEach(el => {
  el.addEventListener('pointerenter', () => show(el));
  el.addEventListener('focus', () => show(el));
  el.addEventListener('click', () => show(el));
  el.addEventListener('pointerdown', () => el.classList.add('pressed'));
  el.addEventListener('pointerup', () => el.classList.remove('pressed'));
  el.addEventListener('pointerleave', () => el.classList.remove('pressed'));
});
document.querySelectorAll('.seg button[data-layer]').forEach(b => b.addEventListener('click', () => { layer = b.dataset.layer; applyLayer(); }));
document.querySelectorAll('.seg button[data-deck]').forEach(b => b.addEventListener('click', () => { deckPair = +b.dataset.deck; applyDeck(); }));
// Keyboard peek: Shift and Alt are tracked independently, the layer the user had
// chosen before the FIRST modifier went down is what comes back once BOTH are up.
const held = { shift: false, alt: false };
let peekBase = null;
function peekLayer() {
  // Compose the held keys with the layer chosen by button: on MODE, holding
  // Shift peeks SHIFT+MODE; on SHIFT, holding Alt peeks SHIFT+MODE.
  if (!held.shift && !held.alt) return peekBase;
  const s = held.shift || peekBase === 'shift' || peekBase === 'shift_mode';
  const m = held.alt || peekBase === 'mode' || peekBase === 'shift_mode';
  return s && m ? 'shift_mode' : s ? 'shift' : 'mode';
}
document.addEventListener('keydown', e => {
  if (e.repeat) return;
  if (e.key !== 'Shift' && e.key !== 'Alt') return;
  if (e.key === 'Alt') e.preventDefault();
  if (peekBase === null) peekBase = layer;
  held[e.key === 'Shift' ? 'shift' : 'alt'] = true;
  layer = peekLayer(); applyLayer();
});
document.addEventListener('keyup', e => {
  if (e.key !== 'Shift' && e.key !== 'Alt') return;
  held[e.key === 'Shift' ? 'shift' : 'alt'] = false;
  layer = peekLayer(); applyLayer();
  if (!held.shift && !held.alt) peekBase = null;
});
window.addEventListener('blur', () => { held.shift = held.alt = false; if (peekBase !== null) { layer = peekBase; peekBase = null; applyLayer(); } });
try { const s = localStorage.getItem('mixtour-pro-layer'); if (s && DATA.layers.includes(s)) layer = s; } catch (_) {}
applyLayer(); applyDeck();
document.querySelectorAll('.seg button[data-layer]').forEach(b => b.addEventListener('click', () => { try { localStorage.setItem('mixtour-pro-layer', layer); } catch (_) {} }));
// swatches
const sw = document.getElementById('swatches');
if (sw) {
  for (let v = 0; v < 64; v++) {
    const r = (v >> 4) & 3, g = (v >> 2) & 3, b = v & 3;
    const css = `rgb(${Math.round(r / 3 * 255)},${Math.round(g / 3 * 255)},${Math.round(b / 3 * 255)})`;
    const d = document.createElement('div');
    d.className = 'sw'; d.style.background = css;
    d.title = `value ${v + 64} (0x${(v + 64).toString(16).toUpperCase()}) full / ${v} (0x${v.toString(16).toUpperCase().padStart(2, '0')}) dimmed: R ${r}/3 G ${g}/3 B ${b}/3`;
    d.innerHTML = `<span>${(v + 64).toString(16).toUpperCase()}</span>`;
    sw.appendChild(d);
  }
}
"""


def control_divs(cs: list[C], with_num: bool) -> str:
    parts = []
    for c in cs:
        base = c.layers["base"].midi
        assert base
        for side, b in placements(c):
            deck = side_deck(side)
            scope = scope_of(c, base)
            p = pct(b)
            shift = (c.fig + "_shift") if "shift" in c.layers else ""
            attrs = (
                f'class="control kind-{c.kind}" role="button" tabindex="0" '
                f'style="left:{p["x"]}%;top:{p["y"]}%;width:{p["w"]}%;height:{p["h"]}%;" '
                f'data-fig="{esc(c.fig)}" data-label="{esc(c.label)}" data-kind="{esc(c.kind)}" '
                f'data-midi-name="{esc(c.fig)}" data-midi-type="{esc(base.type)}" '
                f'data-midi-ch="{chan(base.letter, deck if scope == "side" else 1) if scope != "global" else chan(base.letter)}" '
                f'data-midi-code="{base.code}" data-shift-name="{esc(shift)}" data-layer="both" '
                f'data-side="{side}" data-num="{esc(c.num)}" title="{esc(c.num)} {esc(c.label)}"'
            )
            num = f'<span class="num">{esc(c.num)}</span>' if with_num else ""
            parts.append(f'<div {attrs}>{num}<span class="tag">{esc(c.label)}</span></div>')
    return "\n".join(parts)


def hardware_divs() -> str:
    parts = []
    for idx, (num, label, boxes, _text) in enumerate(HARDWARE):
        for b in boxes:
            p = pct(b)
            parts.append(
                f'<div class="hw" role="button" tabindex="0" data-hw="{idx}" title="{esc(num)} {esc(label)}" '
                f'style="left:{p["x"]}%;top:{p["y"]}%;width:{p["w"]}%;height:{p["h"]}%;">'
                f'<span class="num">{esc(num)}</span></div>'
            )
    return "\n".join(parts)


SCOPE_TXT = {
    "global": "global ch 16",
    "side": "this side's deck",
    "modifier_side": "deck whose MODE is held",
    "active_decks": "follows active decks",
    "pad_bank": "deck the pad bank is assigned to",
}


def reference_table(cs: list[C]) -> str:
    rows = []
    last_sec = None
    for c in cs:
        if c.section != last_sec:
            rows.append(f'<tr class="sec"><td colspan="6">{esc(c.section)}</td></tr>')
            last_sec = c.section
        first = True
        n = len(c.layers)
        for ly in LAYERS:
            f = c.layers.get(ly)
            if not f:
                continue
            m = f.midi
            midi = (
                "hardware only"
                if not m
                else (
                    f"{'OUT ' if m.direction == 'out' else ''}{m.type.upper()} {m.letter} 0x{hx(m.code)} ({m.code})"
                    + (f", LED 0x{hx(m.led)}" if m.led is not None else "")
                    + f" [{SCOPE_TXT[scope_of(c, m)]}]"
                )
            )
            vals = ""
            if m:
                if m.values_in:
                    vals += f"in {m.values_in}"
                if m.values_out:
                    vals += (" / " if vals else "") + f"out {m.values_out}"
            lead = (
                (
                    f'<td class="n" rowspan="{n}">{esc(c.num)}</td><td class="l" rowspan="{n}"><b>{esc(c.label)}</b>'
                    f'<div class="hint">{esc(c.kind)}{", per deck" if c.deck_scoped else ", global"}</div></td>'
                )
                if first
                else ""
            )
            rows.append(
                f'<tr>{lead}<td><span class="pill {ly}">{esc(LAYER_TITLE[ly])}</span></td>'
                f'<td>{esc(f.text)} <span class="src">[{esc(f.src)}]</span></td>'
                f'<td class="n">{esc(midi)}<div class="hint">{esc(vals)}</div></td>'
                + (f'<td rowspan="{n}">{esc(c.led)}</td>' if first else "")
                + "</tr>"
            )
            first = False
    return "\n".join(rows)


def pad_mode_table() -> str:
    rows = []
    for pad, mode, how, shift, src in PAD_MODES:
        rows.append(
            f"<tr><td class='n'>MODE + pad {pad}</td><td><b>{esc(mode)}</b></td><td>{esc(how)}</td>"
            f"<td>{esc(shift)}</td><td class='src'>[{src}]</td></tr>"
        )
    return "\n".join(rows)


def biled_table() -> str:
    color = {
        0x00: "transparent",
        0x01: "#7a1c1c",
        0x02: "#1c3a7a",
        0x03: "#4a1c7a",
        0x7D: "var(--led-red)",
        0x7E: "var(--led-blue)",
        0x7F: "var(--led-purple)",
    }
    return "\n".join(
        f"<tr><td class='n'>0x{hx(v)} ({v})</td><td><span class='led' style='background:{color[v]}'></span>{esc(t)}</td></tr>"
        for v, t in BILED
    )


def hardware_table() -> str:
    return "\n".join(
        f"<tr><td class='n'>{esc(n)}</td><td><b>{esc(l)}</b></td><td>{esc(t)}</td></tr>" for n, l, _b, t in HARDWARE
    )


def channel_table() -> str:
    rows = []
    for letter, desc in CHANNEL_KEY.items():
        chans = (
            "16"
            if letter == "G"
            else ", ".join(f"deck {d} = ch {chan(letter, d)} (0x{chan(letter, d) - 1:02X})" for d in range(1, 5))
        )
        rows.append(f"<tr><td class='n'>{letter}</td><td>{esc(desc)}</td><td class='n'>{esc(chans)}</td></tr>")
    return "\n".join(rows)
