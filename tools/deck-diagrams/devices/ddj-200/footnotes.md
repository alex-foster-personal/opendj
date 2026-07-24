# DDJ-200 diagram footnotes

Bootstrapped from official MIDI message list PDF.

## Status

- Plate cropped from PDF page 1 → `source/plate-top.png`
- Fig inventory extracted heuristically (25 labels)
- MIDI codes in `midi.json` are **stubs** until table parse is completed
- Positions are a grid fallback - refine in red overlay against the plate
- SHIFT twins not yet joined (see skill workflow step 5)

## Next (skill)

1. Parse MIDI tables into real `midi.json` rows with `[Fig]` tags
2. Hand-tune `layout.json` x/y against plate (red overlay)
3. Wire `shift_name` twins
4. `uv run python tools/deck-diagrams/scripts/checksum.py tools/deck-diagrams/devices/ddj-200`
