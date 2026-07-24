# DDJ-REV7 diagram footnotes

Source: official AlphaTheta `DDJ-REV7_MIDI_Message_List_E1.pdf` (archived under
`source/`).

## How to read the plate

- **D\*** = deck transport, jog, loop, key, slot-mode buttons (left deck labeled;
  right deck mirrors same fig codes on ch 2).
- **P\*** = performance pad mode selectors (P1-P4), pads (P5-P12), parameter
  buttons (P13-P14).
- **M\*** = mixer, phones, mic, crossfader, fader curves.
- **E\*** = beat FX / software FX section (REV7 uses **E**, not FLX10-style **F**).
- **B\*** = browser (rotate, back, load).
- Table column **+SHIFT** = distinct MIDI note/CC while hardware SHIFT (M10) held.

## MIDI channel assignment (summary)

| Channel | Category |
|---|---|
| 1-2 | Decks 1-2 (non-pad) |
| 3-4 | Deck slot/pad mode buttons |
| 5-6 | FX (CH1 / CH2 software FX rows) |
| 7 | Browser + global mixer + beat FX selector notes |
| 8 / 10 | Deck 1 / 2 performance pads (unshifted) |
| 9 / 11 | Deck 1 / 2 performance pads (shifted) |
| 16 | MIDI-OUT illumination |

## SHIFT (M10)

Fig **M10** is the hardware SHIFT button (note ch7 code 63). While held, figs
with a `*_shift` twin in `midi.json` send that alternate row (see
`layout.json` `midi.shift_name`). Pad shifted rows use odd channels (9/11).

## Bootstrap cleanup

- False **B6** label removed (was pdftotext picking hex status bytes, not a fig).
- **E1-E13** effect figs added (missing from letter-only bootstrap scan).

## Out of scope (HID / hardware-only)

- Jog display bitmaps (motorized platter screens)
- VU / level meter LED animations
- **M18**, **M20**, **M21**, **M28**, **M32**, **M33**: PDF marks "Hardware Control"
  (no MIDI row); kept on plate with approximate hit targets for completeness
- Right-deck mirror uses same fig ids as left (one label set on plate photo)

## Layout notes

- Coordinates in `layout.json` are percent-of-`plate-top.png` (1597x886).
- One hit target per fig. Pad mode pages and multi-mode pad rows stay in
  `midi.json`; diagram shows the physical control only.
- MSB encoder rows stored as primary `cc` code from MSB line (LSB companion in PDF).

## Checksum waivers

(none - `checksum.py` PASS as of table parse)

## Remaining gaps

1. **Overlay geometry**: first-pass percent coords; open `overlay.html` and nudge
   clusters (especially right-deck mirror, mic column, FX levers).
2. **Pad mode layers**: P1-P4 / P13-P14 have many mode-specific MIDI rows beyond
   the primary + shift pair wired in layout.
3. **Dual-deck channel expansion**: table rows show ch 1/2, 3/4, 8/10; diagram
   stores deck-1 primary channel with convention note only.
