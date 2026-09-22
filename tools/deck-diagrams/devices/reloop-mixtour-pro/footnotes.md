# Reloop MIXTOUR PRO footnotes

The retained `midi.json` and `reference.md` describe the controller mapping. The manufacturer image, image-backed layout, diagrams and renderer are withheld from this public copy pending redistribution permission. Diagram regeneration and hover/geometry verification for this device are unavailable here.

Descriptions of those omitted files below document the legacy private rendering workflow; they are not public build instructions. Generic MIDI mapping remains separate from that workflow.

## Sources

Reloop publishes no MIDI map on the product page (tier 2 in the catalog). The map used
here is the manufacturer's own `midimap_MIXTOURPRO.pdf` (2 pages: MIDI map + LED colour
coding map) as redistributed in github.com/sayak-brm/ReloopMixxxtourPro, cross-checked
against that repository's hardware captures (MAPPING_NOTES.md, commit 45ce7cda, Mon 24
Aug 2026). Functions come from the operation manual REV 1.0 (246959_Reloop_IM.pdf, English
pp. 3-7), the silkscreen labels, and Reloop's "Laidback Luke Signature Features" sheet,
which is where www.reloop.com/reloop-mixtour-pro-mapping redirects. Every function and
every address in the outputs carries one of the tags [IM] [LBL] [LL] [MM] [HW] [FORUM].

The three PDFs sit in `source/` and are gitignored (vendor copyright, same rule as
`docs/controller/reference/`). sha256 for provenance:

- 246959_Reloop_IM.pdf b6c416f8440a9b90e8578ae15921def2a9c27948cc17232798dcf542396284af
- Reloop-LAIDBACK_LUKE_SIGNATURE_FEATURES_V1.pdf 220b6316fedab9dd05db4b946e01c96feac9d81594d85d2b8ca966c0610ce1c8
- midimap_MIXTOURPRO.pdf as in the community repository at commit 45ce7cda

`source/plate-top.png` is a crop of the manual's page-3 designation figure (top plate
plus rear, front and side strips), 1010 x 1743 px. Coordinates in `layout.json` are
percent of that image.

## Fig ids and layers

`fig` ids are snake_case control names (Reloop has no plate fig codes). Layers are
`base`, `shift`, `mode`, `shift_mode`; each layer is a DIFFERENT note or CC on the
wire, never a modifier bit, so every layer is a separate row in `midi.json` named
`<fig>`, `<fig>_shift`, `<fig>_mode`, `<fig>_shift_mode`. `layout.json` joins them via
`shift_name`, `mode_name`, `shift_mode_name`, all three declared in the shared
`control-layout.schema.json`; `checksum.py` requires each twin to resolve to a midi.json
row that is on that layer, and the generic `generate_html.py` shows them.

## Channels and scope

The map keys channels by letter: N deck transport+mixer (ch 1-4), P deck pads/mode/load
(ch 5-8), E deck FX unit (ch 9-12), G global (ch 16). `midi.json` holds deck-1 rows.
MODE+LOAD switches a side to deck 3 or 4, which adds 2 to the N/P/E channel; `deck.html`
has a 1/2 vs 3/4 toggle that re-derives the numbers.

Which deck an address is on is a property of the LAYER, not the control, so every
`midi.json` row and every `layout.json` control carries a `scope` (shared schema enum):

- `side`: a mirrored control's own deck (left = 1/3, right = 2/4). `layout.json` carries
  the side's channel and an explicit `side` field; side is never inferred from the
  number of geometry boxes.
- `global`: G channel, one physical control.
- `modifier_side`: a center control's MODE layer (BROWSE MODE+turn, FX DRY/WET MODE+turn,
  MODE+PARAM) goes to the deck whose MODE button is held; the card shows both sides.
  SHIFT+PARAM is on an E channel too but SHIFT has no side, so its deck is ASSUMED.
- `active_decks`: SHIFT+crossfader fader start follows the deck active on each end.
- `pad_bank`: all eight pad notes exist on EVERY deck's P channel [HW: the Mixxx map
  binds 0x14..0x1B on 0x94..0x97 and dispatches by channel]. A pad's channel is the
  deck its bank is assigned to (unsplit: the deck whose MODE picked the mode; SPLIT:
  pads 1-4 the left deck, 5-8 the right deck), not a property of its column.
  `layout.json` carries the left bank's deck-1 channel with `scope: pad_bank`.

## Corrections to the manufacturer map applied ([HW])

- FX DRY/WET is ONE global CC (G 02), not one per deck. The E 03 column is MODE + turn.
- MODE+PARAM sends E 0B / 0C (the map's MODE column), verified on hardware; E 0A / 0D is
  the SHIFT column.
- SHIFT+pad sends P 1C..23 and lights a SEPARATE LED bank; the base pad note has no
  effect on the shift display. Same for the transport MODE / SHIFT+MODE banks.
- VU meter CC 1F is a 7-segment discrete value 00..06, not a scaled level.

## Assumed (not documented anywhere consulted)

- Physical order of pads 1-8 for the base notes P 14..1B: column-major (1-4 left column
  top to bottom, 5-8 right column), inferred from the hardware-captured MODE+pad
  mode-select order. Verify with the learn log when the unit is on a bench.
- djay meaning of SHIFT+SPLIT, SHIFT+MODE, SHIFT+PARAM, SHIFT+BROWSE(turn), MODE+DRY/WET:
  addresses exist in the map, functions are stated nowhere. Shown as "not documented".

## Out of scope

Hardware-only items are drawn as gray dashed outlines in `deck.html` but kept OUT of
`layout.json` so the checksum stays an honest MIDI double-entry: FILTER ON/OFF LED (11a),
the SHIFT LED, and the rear/front/side items 26-31 (headphones out, master out, the two
USB-C ports, power, Kensington). The MONO/STEREO switch (25) IS in the map (G note 7F) and
is a layout control.

WAIVE_FIG: none
