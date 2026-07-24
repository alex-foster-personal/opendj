# Scale to ~100 controllers

Tracking: https://github.com/maintainer/music-dj-tools/issues/371

## Gate (must hold before mass fan-out)

1. FLX10 checksum exits 0 and overlay reviewed against plate.
2. Skill documents letter figs, section figs (`1-1`), Reloop named figs, AlphaTheta URL casing.
3. At least 5 diverse devices checksum-green (Pioneer letter, Pioneer section, Reloop named, plus Denon tier-2 stub path noted).
4. `bootstrap_from_midi_pdf.py` + `generate_html.py` + `checksum.py` stay the toolchain.
5. Catalog `popularity_rank` used as queue order (quick ranks OK until deep research).

## Current wave

| id | figs | checksum | notes |
|---|---|---|---|
| ddj-flx10 | 79 | PASS | reference implementation |
| ddj-flx4 | 40 | PASS | section figs |
| ddj-flx6 | 49 | PASS | section figs |
| ddj-1000 | 69 | PASS | letter figs |
| ddj-rev7 | 83 | PASS | letter + E* FX figs |
| reloop-mixtour | 28 | PASS | named figs / schematic |
| denon-prime4 | 0 | stub | tier-2 Mixxx path |

## After gate

- Parallel subagents: one device each under `devices/<id>/`
- Prefer `doc_tier: 1` then tier-2 Mixxx ingest
- Do not expand past ~5 new devices per wave without hardening overlay-nudge + pad-mode layers in the skill
