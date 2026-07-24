# DDJ-FLX4 diagram footnotes

Parsed from official MIDI message list PDF (`source/DDJ-FLX4_MIDI_Message_List_E1.pdf`).

## Channel assignment

- **ch1-2**: deck transport, trim/EQ per deck, channel cue/fader
- **ch5-6**: beat FX (FX select, beat buttons, level/depth, FX on/off)
- **ch7**: browse/load, master level/cue, crossfader, mic/headphone, smart CFX/fader
- **ch8-11**: performance pads (deck1 unshifted/shifted = 8/9, deck2 = 10/11)

## SHIFT encoding

Hardware SHIFT uses **different note/CC rows**, not a software modifier bit. Layout `shift_name` joins primary + `*_shift` twin from `midi.json`.

## Out of scope (documented, not layout targets)

- Pad mode matrix rows under fig **5-5** (8 pads x 7 modes x deck/shift) stay in `midi.json` only
- Jog touch / wheel-side secondary rows for **1-4** (platter touch notes, wheel CC 33)
- MSB/LSB companion CC rows for knobs/faders (primary entry uses MSB code)
- **3-15** CH LEVEL METER is MIDI-OUT illumination (included as fig for checksum parity)
- HID jog ring / display bitmaps

## Checksum

```sh
uv run python tools/deck-diagrams/scripts/checksum.py tools/deck-diagrams/devices/ddj-flx4
```

Expected: 40 figs, PASS.
