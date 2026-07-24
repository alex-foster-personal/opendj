# DDJ-FLX10 diagram footnotes

Source: official AlphaTheta `DDJ-FLX10_MIDI_Message_List_E1.pdf` (archived under
`source/` and `docs/controller/reference/`).

## How to read the plate

- Colored callouts **D\*** = deck, **P\*** = performance pads / pad modes,
  **M\*** = mixer / master / mic / headphones, **F\*** = FX, **B\*** = browser.
- The MIDI list labels the **left** deck in detail; the right deck reuses the
  same fig codes on mirrored hardware (deck channel 2/4).
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

## SHIFT (D25)

Fig **D25** is the hardware SHIFT button (`shift_button`, note ch1 code 63).
While SHIFT is held, many figs send their `*_shift` twin (see `layout.json`
`midi.shift_name`). Pad modes move to odd channels (9/11/13/15).

## Out of scope (HID / not in MIDI list as controllable UI)

- Jog display bitmaps
- VU / level meter LED animations beyond CC meter rows
- Stem FX indicator lights over undocumented HID

## Layout notes

- Coordinates in `layout.json` are percent-of-`plate-top.png`.
- One hit target per fig. Pad **modes** (hot cue / pad fx / …) expand in
  `midi.json` as many rows sharing P1-P8; diagram shows the physical pad.
- **M16** (LINE/PHONO) is a rear-panel switch; drawn as a callout on the plate.

## Checksum waivers

(none yet - use `WAIVE_FIG: XX` lines if a fig is intentionally omitted)
