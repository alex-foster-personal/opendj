Layout metrics: topbar 28 px, waveform rows 43 px, deck art 75 px, overview strip 28 px, bottom-bar inset 6 px, 220 px app sidebar.

Performance app stacks top to bottom: `--rb-topbar-h` topbar, one `--rb-waverow-h` row per deck, deck headers with
`--rb-deck-art` artwork and a `--rb-strip-h` overview strip, then the browser table filling the remaining height. Nothing
floats over it: the browser's bottom bar is inset from the left edge by `--rb-perf-nav-w`.

App chrome: `.app-shell` is `grid-template-columns: 220px 1fr`; `.sidebar` padding 1 rem, `.content` padding 1.5 rem
with horizontal overflow rather than reflow.

Spacing inside `.perf-root` is pixel-based (2 to 12 px, radii 2 to 4 px); app chrome is rem-based (0.4 to 1.5 rem,
radii 6 to 8 px). Do not mix the two scales on one surface.
