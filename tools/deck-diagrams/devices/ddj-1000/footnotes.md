# DDJ-1000 diagram footnotes

Source: official AlphaTheta `DDJ-1000_MIDI_Message_List_E1.pdf` (archived under
`source/`).

## How to read the plate

- Colored callouts **D\*** = deck, **P\*** = performance pads / pad modes,
  **M\*** = mixer / master / mic / headphones, **F\*** = FX, **B\*** = browser.
- Plate labels use **-L** / **-R** suffixes for mirrored left/right deck hardware;
  the diagram uses one fig code per control type (e.g. **D1** covers both decks).
- Table column **+SHIFT** means a distinct MIDI message (different note/CC),
  not a software "shift held" flag on the same code.

## MIDI channel assignment (summary)

| Channel | Category |
|---|---|
| 1-4 | Decks 1-4 (non-pad) |
| 5 | Effect |
| 7 | Browser + global mixer |
| 8/9 | Deck 1 pads unshifted / shifted |
| 10/11 | Deck 2 pads |
| 12/13 | Deck 3 pads |
| 14/15 | Deck 4 pads |
| 16 | MIDI-OUT illumination |

## SHIFT (D19)

Fig **D19** is the hardware SHIFT button (`shift_button`, note ch1 code 63).
While SHIFT is held, many figs send their `*_shift` twin (see `layout.json`
`midi.shift_name`). Pad modes move to odd channels (9/11/13/15).

DDJ-1000 places SHIFT at **D19** (FLX10 uses **D25** for the same role).

## Out of scope (HID / not in MIDI list as controllable UI)

- Jog display bitmaps (MIDI-OUT ch16 illumination section)
- VU / level meter LED animations beyond CC meter rows (**M21**, **M22**)
- Right-deck mirror hit targets (same fig codes as left)

## Layout notes

- Coordinates in `layout.json` are percent-of-`plate-top.png` (1118 x 615).
- Positions seeded from FLX10 overlay tuning, scaled for DDJ-1000 plate geometry.
- One hit target per fig. Pad **modes** (hot cue / pad fx / beat jump / sampler /
  keyboard / beat loop / key shift) expand in `midi.json` as many rows sharing
  P1-P8; diagram shows the physical pad only.
- **M16** (LINE/PHONO) is a rear-panel switch; drawn as a callout on the plate.
- **F1** maps four COLOR FX parameter knobs (ch1-4) under one fig.
- **F8** FX SELECT encoder exposes many NOTE positions; layout primary is LOW CUT.

## Build / verify

```sh
uv run python tools/deck-diagrams/scripts/build_ddj1000_layout.py
uv run python tools/deck-diagrams/scripts/generate_html.py tools/deck-diagrams/devices/ddj-1000
uv run python tools/deck-diagrams/scripts/checksum.py tools/deck-diagrams/devices/ddj-1000
```

## Checksum waivers

(none - use `WAIVE_FIG: XX` lines if a fig is intentionally omitted)

## Remaining gaps

1. Red-overlay coordinates need visual pass against `plate-top.png` (seeded from
   FLX10, not yet hand-nudged per DDJ-1000 cluster).
2. **F8** FX SELECT and **F9** CH SELECT encoders have many NOTE rows; only
   primary positions are wired in `midi.json`.
3. Pad-mode page layers (hot cue page 2, pad fx 2, keyboard, beat loop, key
   shift) are in `midi.json` but not separate diagram layers yet.
