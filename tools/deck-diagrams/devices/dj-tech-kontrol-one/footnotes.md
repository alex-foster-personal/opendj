# DJ-Tech Kontrol One diagram footnotes

Practice replica (issue #10860, ADR-0137). Path B: vendor User Manual PDF unobtainable on CI.

## Source

- Wire list: Mixxx `DJ-Tech Kontrol One.midi.xml` and `DJ-Tech-Kontrol-One-scripts.js` (GPL-2.0-or-later),
  tier 2 of (private path). Ingest sha256
  8f2c4249a6ac9b32317d1e74e0b25a5a32a724c1c75532c779dd891e7c99a1bd (`midi.json` in this dir).
- Upstream: https://github.com/mixxxdj/mixxx/blob/main/res/controllers/DJ-Tech%20Kontrol%20One.midi.xml
- Local archive: `tools/deck-diagrams/devices/dj-tech-kontrol-one/midi.json` (from the XML ingest)

The vendor MIDI MAP table (manualslib id 1387269, pages 14-15) was not downloaded: manualslib is
Cloudflare-blocked and dj-tech.com legacy URLs 404. `DJ_TECH_KONTROL_ONE_MAP` is hints-only with
`live: false` and is not in the engine export.

## Status

- Panel trace: straight-on product photo (gearclubdirect listing), image sha256
  b6ec4b1ee9abe30c266be5b02307194c08fb431d1516534461dfcf29ad984528. Image not committed.
- Panel wires use channel 1 (deck 1); DECK SELECT is unwired (`midi: null`).
- No shift twin wires: SHIFT is a software modifier; `shift_midi` is always null on the panel.
- `layout.json` and `midi.json` are the older Mixxx auto-grid ingest; the practice pair is
  `panel.json` + `panel.reference.json` from `build_dj_tech_kontrol_one_panel.py`.
- No `device.json`: practice-only map (same rule as Numark Mixtrack Pro FX and Traktor S4 MK3).

## License note

Mixxx mappings are GPL. This diagram consumes MIDI numbers for teaching/reference;
retain GPL notice if redistributing derived mapping work.
