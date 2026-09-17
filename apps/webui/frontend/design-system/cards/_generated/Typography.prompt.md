Type: system UI stack (no webfont), dense 10 to 13 px performance scale, and the Anybody 800 wordmark subset.

- `--rb-font` is the system stack; body text never loads a webfont (offline-first app).
- `--rb-fs-label: 10px` control labels, `--rb-fs-browser: 11px` browser table, `--rb-fs-deck-title: 13px` deck titles.
- App chrome: browser default 16 px base, tables 0.9 rem, topbar 0.85 rem, chips 0.75 rem.
- Monospace (`ui-monospace, SFMono-Regular, Menlo, monospace`) for identifiers, timestamps, share URLs.
- Wordmark: `font-family: "Anybody Wordmark"`, weight 800, uppercase "OPEN DJ". The shipped subset has only the
  glyphs O P E N D J o p e n d j and space; any other text in that face falls back to the system font.
