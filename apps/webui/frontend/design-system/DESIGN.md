# Open DJ DESIGN.md

Source of truth for the Open DJ visual language, in the nine-section DESIGN.md format Claude Design
reads. Every value below is copied from the shipped stylesheets (`src/app.css`,
`src/lib/rb/theme.css`); when they disagree, the stylesheets win and this file is stale.

## 1. Visual theme and atmosphere

Near-black hardware console. The performance surface imitates rekordbox: dense, flat, low-glow, every
pixel earning its place, blue for "active", green for "loaded", red for "cue" and "danger". App chrome
pages (library, sets, settings) are the same darkness with a warm orange accent and roomier rem-based
spacing. Nothing is playful; the tone is a professional instrument that works offline at 3 a.m. in a
dark booth. A warm light theme exists for the performance surface for daylight use.

## 2. Color palette and roles

App chrome (`:root`):

- `--bg: #0b0d11` page background
- `--surface: #121720` sidebar, topbar, cards, inputs
- `--border: #1c222c` dividers and control outlines
- `--fg: #e6e9ef` primary text
- `--muted: #9aa4b2` secondary text, table headers
- `--accent: #ffb43a` orange: links, primary button, filled stars, sidebar title
- `--accent-dim: #7a5a1f` empty stars
- `--danger: #ff5a5a` errors, warning banner
- `--warning: #e8a13a` the middle rung of the toast scale, separated from danger by lightness so it
  survives the two common color-blindness types

Performance surface (`.perf-root`, dark):

- `--rb-bg: #0d0f12` window, `--rb-panel: #14171d` panel, `--rb-panel-raised: #1a1e25` buttons and chips
- `--rb-border: #23282f` dividers
- `--rb-text: #c8cdd2` primary, `--rb-text-dim: #838990` secondary (measured 4.5:1 on raised panels)
- `--rb-accent: #2f6fd6` active blue for toggles, faders, lit buttons; `--rb-accent-glow` its glow
- `--rb-green: #35c04f` loaded-track rows and loop-out chip
- `--rb-orange: #e8a13a` waveform lows, looped fader line
- `--rb-wave-mid: #3d7dd9`, `--rb-wave-high: #cfe0f2` waveform mids and highs
- `--rb-red: #d0342c` cue markers, position tick, off-tempo tint
- `--rb-yellow: #e5c33a` license badge; master-deck gold is `#c9b35a` on `#1a1608`
- `--rb-select: #1d3f73` selected row

Light theme (`html[data-theme='light'] .perf-root`): `--rb-bg: #f7f3eb`, `--rb-panel: #fffdf8`,
`--rb-text: #27241f`, `--rb-text-dim: #5d574e`, `--rb-accent: #175ea8`, `--rb-green: #1d7035`,
`--rb-red: #ad2420`, `--rb-select: #d6e5f5`. Every text/surface pair stays AA.

## 3. Typography rules

- UI face: the system stack `-apple-system, BlinkMacSystemFont, "Inter", "SF Pro Text", "Segoe UI",
  Roboto, sans-serif`. No webfont is shipped for UI text: the app must render offline.
- Performance scale: `--rb-fs-label: 10px` control labels, `--rb-fs-browser: 11px` browser table,
  `--rb-fs-deck-title: 13px` deck titles. Nothing larger inside `.perf-root` except the tempo readout.
- App chrome: browser default 16 px base; tables 0.9 rem, topbar 0.85 rem, chips 0.75 rem, sidebar h1 1 rem.
- Monospace for identifiers, timestamps and share URLs: `ui-monospace, SFMono-Regular, Menlo, monospace`.
- Wordmark: Anybody 800, width 150, ALL CAPS ("OPEN DJ"), shipped as a 1.6 KB subset. Only the wordmark
  uses it. In the browser panel the wordmark is `--rb-text-dim`, 10 px, weight 600, 0.5 px tracking.

## 4. Component stylings

- App button: raised `#1a212c` fill, 1 px `--border`, 6 px radius, 0.4 x 0.9 rem padding; hover `#222a38`;
  `.primary` is solid `--accent` with `#0b0d11` text.
- Inputs: `--surface` fill, `--border` outline, 6 px radius, inherit font.
- Chip: pill (999 px), `#1a212c` fill, `--muted` text, 0.75 rem.
- Toast: `--surface` card with `--border`, 6 px radius, 240 px min width; severity is the border color
  (`warn` amber, `error` red), never a fill.
- Lit button (`.rb-lit-button`): `--rb-panel-raised` fill, 2 px radius, 10 px label, `--rb-text-dim`;
  `.lit` is white on `--rb-accent` with a 6 px glow; `.master-btn.lit` is soft gold.
- Knob: 26 px, dark radial ring, 1 px border, 2 x 9 px blue indicator rotated by inline transform.
- Fader: 30 px wide vertical track (3 px, near-black) with a 12 px `#2a2f37` thumb carrying a 2 px blue
  line; `.playing` pulses the line, `.looped` turns it orange.
- Stars (`.rb-star`): `#3a3f46` empty, `--rb-text` filled, `#c8cdd2` preview.
- Table rows: `.rb-row-selected` blue highlight, `.rb-row-loaded` green text, `.rb-row-master` gold bold.
- Inert control: keep it in place, `disabled`, `.rb-inert` (opacity 0.75, default cursor), tooltip
  `not implemented - see PARITY-TODO`.

## 5. Layout principles

- App shell: CSS grid, 220 px sidebar + fluid content, sidebar padding 1 rem, content 1.5 rem.
- Performance app, top to bottom: 28 px topbar (`--rb-topbar-h`), one 43 px waveform row per deck
  (`--rb-waverow-h`), deck headers with 75 px artwork (`--rb-deck-art`) and a 28 px overview strip
  (`--rb-strip-h`), then the browser table taking the remaining height. Nothing floats over it: the browser's
  bottom bar starts `--rb-perf-nav-w: 6px` from the left edge.
- Density: inside `.perf-root` spacing is 2 to 12 px and radii are 2 to 4 px; app chrome uses 0.4 to
  1.5 rem and 6 to 8 px radii. Never mix the scales on one surface.
- Decks 1 and 2 are primary: decks 3 and 4 use the lighter `--rb-waverow-secondary` fill so they recede.

## 6. Depth and elevation

Flat by default. Depth is expressed with one-pixel borders and surface steps (`bg` < `panel` <
`panel-raised`), not shadows. The only glows are functional: the lit-button glow (`--rb-accent-glow`)
and the master gold glow. Modals dim the page with `rgba(0,0,0,0.75)` and sit on a `--surface` panel
with an 8 px radius. Sticky table headers use the page background, not a shadow.

## 7. Do's and don'ts

Do:

- Put every performance element inside `.perf-root`; use `--rb-*` there and `:root` tokens elsewhere.
- Give every number a `title` with its unit and denominator.
- Render unbuilt controls inert with the PARITY-TODO tooltip.
- Keep both themes AA; pick colors from the palette.
- Use American English.

Don't:

- Change the app-wide orange accent or make the performance surface orange.
- Add drop shadows, gradients (other than the knob ring) or rounded 8 px+ corners inside `.perf-root`.
- Fake data or wire placeholder handlers to make a screenshot look live.
- Ship a webfont for UI text; the wordmark subset is the only font.
- Print a placeholder number when a value is unknown: print `--` and explain in the title.

## 8. Responsive behavior

Desktop-first, min 1280 px wide for the performance surface; it does not reflow for phones. App chrome
pages scroll horizontally in `.content` rather than collapsing tables. Touch targets on the performance
surface are small by design (hardware controllers drive it); the desktop nav and sidebar links are the
only 32 px+ targets. Reduced motion disables the fader pulse, the off-tempo tint and the launch slide.

## 9. Agent prompt guide

- "Build a performance-mode screen": wrap in `.perf-root`, use `--rb-*` tokens, 10 to 13 px type, panels
  with `.rb-panel`, controls from section 4, browser table with the row-state classes.
- "Build a library or settings page": use the `.app-shell` grid, `:root` tokens, `.topbar`, `table.library`,
  `.chip`, app buttons and inputs.
- "Show an error or warning": `.toast.error` or `.toast.warn`, or `.banner-warning` for page-wide alerts.
- "Add a feature that does not exist yet": render it inert with the PARITY-TODO tooltip.
- Always: hover titles on numbers, AA pairs, reduced-motion branch, American spelling.
