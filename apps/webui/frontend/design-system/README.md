# Open DJ design system

Open DJ is a local-first DJ application: a rekordbox-style **performance** surface (decks, waveforms,
mixer, track browser) plus lighter **app chrome** pages (library, sets, settings, admin). The
product is a SvelteKit app. This design system ships **no JavaScript components**: it is the CSS the
app actually runs, verbatim, plus documented markup recipes. Build designs from plain HTML and the
classes below; do not invent a component API.

## How to use it

1. `styles.css` is already loaded in every design. It pulls in `tokens/app.css` (app chrome, `:root`),
   `tokens/rb-theme.css` (performance palette and shared controls, scoped to `.perf-root`) and
   `fonts/fonts.css` (the wordmark face only).
2. **Performance surfaces must sit inside `<div class="perf-root">`.** Every `--rb-*` token and every
   `.rb-*` class is scoped to that wrapper on purpose: the app-wide accent is orange and must stay
   orange, while the performance UI is blue-accented. Outside `.perf-root` the `--rb-*` variables do
   not exist.
3. App chrome pages use the `:root` tokens directly: `--bg`, `--fg`, `--muted`, `--accent`, `--surface`,
   `--border`, `--danger`, `--warning`.
4. Light mode for the performance surface is `<html data-theme="light">`; the same `--rb-*` names
   resolve to the warm light palette. Dark is the default and the primary target.
5. Type is the system stack (no webfont), deliberately dense: 10 to 13 px inside `.perf-root`,
   browser-default 16 px base with 0.85 to 0.9 rem table text on app chrome. The only shipped font is the
   wordmark subset, family `Anybody Wordmark`, used for the words "OPEN DJ" and nothing else.

## Markup recipes

### App chrome (`:root` scope)

- Shell: `<div class="app-shell"><aside class="sidebar"><h1>Open DJ</h1><nav><a class="active">Library</a>...</nav></aside><main class="content">...</main></div>`
- Buttons: `<button>Cancel</button>`, `<button class="primary">Save</button>`. Disabled buttons use the `disabled` attribute.
- Inputs: bare `<input>`, `<select>`, `<textarea>` are styled globally.
- Chip: `<span class="chip">house</span>`.
- Toasts: `<div class="toast-stack"><div class="toast">Saved</div><div class="toast warn">...</div><div class="toast error">...</div></div>`.
  Severity is border color only: `--warning` amber, `--danger` red.
- Topbar: `<div class="topbar">...</div>`; full-width alert: `<div class="banner-warning">...</div>`.
- Library table: `<table class="library">` with sticky `thead th`, hover rows, `<span class="star filled">★</span>` for ratings.
- Modal: `<div class="conflict-dialog"><div class="panel">...<div class="actions"><button>Keep mine</button><button class="primary">Use theirs</button></div></div></div>`.

### Performance controls (`.perf-root` scope)

- Panel: `<div class="rb-panel">` (panel surface + 1 px `--rb-border`).
- Lit button: `<button class="rb-lit-button">SYNC</button>`; active state adds `lit` (blue fill + glow); the
  master-deck button is `rb-lit-button master-btn lit` (soft gold, beats the generic blue).
- Knob: `<div class="rb-knob"><div class="rb-knob-indicator" style="transform: rotate(45deg)"></div></div>`;
  rotate the indicator between -135 and 135 deg.
- Fader: `<div class="rb-fader"><div class="rb-fader-track"></div><div class="rb-fader-thumb" style="top: 40%"></div></div>`;
  add `playing` to pulse the thumb line, `looped` to turn it orange.
- Stars: five `<span class="rb-star">★</span>`, `filled` for set stars, `preview` for the hover preview.
- Track rows: `tr.rb-row-selected` (blue full-row highlight), a loaded track's cells use `rb-row-loaded`
  (green), the master deck's row is `tr.rb-row-master` with `.c-title` / `.c-artist` cells (gold, bold).
- Waveform row: `<div class="rb-waverow">` at `--rb-waverow-h`; decks 3 and 4 fill with
  `--rb-waverow-secondary` so decks 1 and 2 keep the eye. The finished-track eject chip is
  `<button class="finished-eject">` inside the row.
- Deck off-tempo warning: a deck (`.rb-deck`, positioned) containing `.jog-off-tempo` gets a pulsing red tint.

## House rules (every surface)

- **Controls without a real data source render inert**: keep them in their real position, add the
  `disabled` attribute and the `rb-inert` class, and set both `title` and `aria-label` to exactly
  `not implemented - see PARITY-TODO`. Never wire a fake handler or placeholder value.
- **Every numeric readout carries a hover `title`** naming the quantity and its unit
  ("Effective 256 kbps", "analysis coverage: 4/9"), including graphical readouts like dot grids.
- **No mocked data**: a design may show realistic sample content, but a control that would need a
  backend it does not have is inert, not faked.
- **Contrast**: every text-on-surface token pair is WCAG AA (4.5:1) at the token level. Do not add
  component-local colors that break a pair; pick from the palette.
- **Motion**: only compositor-friendly properties, and every animation has a
  `prefers-reduced-motion: reduce` branch that disables it.
- **American English** in all copy and identifiers: color, analyze, behavior, catalog, license.
- Spacing is tight and pixel-based inside `.perf-root` (2 to 12 px, radii 2 to 4 px); app chrome uses rem
  spacing (0.4 to 1.5 rem, radius 6 to 8 px). Do not mix the two scales on one surface.

## What this design system is not

There is no React or Svelte component bundle here. `_ds_bundle.js` is an empty namespace so the
app's self-check passes. If you need behavior, write plain HTML and CSS with these classes and tokens;
the engineering team maps it onto the Svelte components one to one.
