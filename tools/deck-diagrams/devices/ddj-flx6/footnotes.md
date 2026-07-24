# DDJ-FLX6 diagram footnotes

Source: official AlphaTheta `DDJ-FLX6_MIDI_Message_List_E1.pdf` (archived under
`source/` and `docs/controller/reference/`).

## How to read the plate

- Section-style fig codes: **1-1** through **1-17** (deck), **2-1** through **2-6**
  (beat FX), **3-1** through **3-14** (mixer), **4-1** through **4-7** (browser),
  **5-1** through **5-5** (performance pads / pad modes).
- The MIDI list labels the **left** deck pair (decks 1/3) in detail; the right pair
  (decks 2/4) reuses the same fig codes on mirrored hardware (deck channel 2/4).
- Table column **+SHIFT** means a distinct MIDI message (different note/CC),
  not a software "shift held" flag on the same code.

## MIDI channel assignment (summary)

| Channel | Category |
|---|---|
| 1-4 | Decks 1-4 (non-pad) |
| 5 | Merge FX left + Beat FX bank 1 |
| 6 | Merge FX right + Beat FX bank 2 |
| 7 | Browser + global mixer / phones / mic / crossfader |
| 8/9 | Deck 1 pads unshifted / shifted |
| 10/11 | Deck 2 pads |
| 12/13 | Deck 3 pads |
| 14/15 | Deck 4 pads |
| 12 | Jog / merge illumination MIDI OUT |

## SHIFT (1-3)

Fig **1-3** is the hardware SHIFT button (`shift_button`, note ch1 code 63 on deck 1).
While SHIFT is held, many figs send their `*_shift` twin (see `layout.json`
`midi.shift_name`). Pad modes move to odd channels (9/11/13/15).

## Out of scope (HID / not controllable via MIDI-IN)

- Jog ring illumination (MIDI OUT ch12)
- Merge FX ring illumination (MIDI OUT ch5/6)
- CH level meter LED animation (MIDI OUT ch1-4 CC2)
- Full performance pad mode matrix (8 pads x 8 modes) as separate hit targets

## Layout notes

- Coordinates in `layout.json` are percent-of-`plate-top.png` (1118x614 crop).
- One hit target per fig. Pad **modes** (5-1..5-4) are separate figs; pad **cells**
  share fig **5-5** with mode rows retained in `midi.json`.
- 14-bit knobs/faders store the MSB code in `midi.code`; see PDF for LSB pairs.

## Checksum waivers

(none - `checksum.py` PASS with 49 figs)

## Remaining gaps

1. Red-overlay pixel review: coords are hand-estimated from the plate photo; expect
   1-2 px nudges on jog, merge FX knobs, and mixer fader strip.
2. Right-deck mirror: diagram draws left-deck representative positions only (same fig
   covers decks 2/4 on the physical unit).
3. Pad mode depth: `5-5` holds one pad-grid target plus sample mode rows in
   `midi.json`; individual pad cells and all mode pages are not separate figs.
