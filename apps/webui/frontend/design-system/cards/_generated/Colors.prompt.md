Color tokens: app chrome `:root` palette (orange accent), performance `.perf-root` palette dark and light (blue accent).

Generated from `src/app.css` and `src/lib/rb/theme.css`; every swatch is a shipped custom property.

Use `--bg / --surface / --border / --fg / --muted / --accent / --danger / --warning` on app chrome pages.
Inside `<div class="perf-root">` use `--rb-bg / --rb-panel / --rb-panel-raised / --rb-border / --rb-text /
--rb-text-dim / --rb-accent / --rb-green / --rb-orange / --rb-red / --rb-yellow / --rb-select` and the waveform
band colors `--rb-orange` (lows), `--rb-wave-mid`, `--rb-wave-high`.

Light theme: `<html data-theme="light">` remaps the same `--rb-*` names. Every text/surface pair is WCAG AA
in both themes; do not add component-local colors that break a pair.
