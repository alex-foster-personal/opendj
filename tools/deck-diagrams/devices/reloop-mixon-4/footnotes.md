# Reloop Mixon 4 diagram footnotes

## Source

- Reloop "MIXON 4 MIDI MAP": https://www.reloop.com/media/custom/upload/Reloop-Mixon4_MIDI-Map.pdf
- Pages 1 to 3 are the control tables, page 4 LED value tables and SysEx, page 5 the RGB LED map (LED pages out of scope).
- PDF kept local only (manufacturer copyright). `source/plate-top.png` is a crop of the top-view product photo
  on https://www.reloop.com/reloop-mixon-4 (236534_Reloop_TP.png).

## Channels

- The vendor table gives a channel nibble n per deck (status 0x9n / 0xBn / 0xEn); `ch` in midi.json is n + 1.
- Deck controls: deck 1/2/3/4 = n 4/5/6/7. Left deck is deck 1 (deck 3 in layer `deck_cd`), right deck is deck 2 (deck 4).
- Pads: deck 1/2/3/4 = n 0/1/2/3.
- Mixer channel k uses n = 3 + k (order on the unit CH3 CH1 | master | CH2 CH4).
- Effect unit 1 (left) n 8, unit 2 (right) n 9. Crossfader, CF curve, sampler, cue vol, cue mix, TRAX, BACK, SHIFT,
  QUANTIZE: n 0Ah.
- DECK button: note 0x12; the vendor channel column reads 6 7 4 5 (deck 1 sends on n 6). +SHIFT DECK is note 0x52 on n 4 to 7.
- MASTER VOL, BOOTH VOL and FILTER LED are hardware only (no MIDI).

## SHIFT

- Rows with "-" in the vendor SHIFT column have no shift twin. HOT CUE mode button shift is hardware.
- +SHIFT channel fader sends FADER START UP / DOWN notes 0x5D / 0x5E.

## Pads

- midi.json holds HOT CUE mode: notes 0x00 to 0x07, +SHIFT 0x40 to 0x47.
- LOOP ROLL 0x08 to 0x0F (shift 0x48 to 0x4F), SAMPLER 0x10 to 0x17 (0x50 to 0x57), SLICER 0x18 to 0x1F (0x58 to 0x5F),
  CUE LOOP 0x20 to 0x27 (0x60 to 0x67), SAVED LOOPS 0x28 to 0x2F (0x68 to 0x6F), PITCH PLAY 0x30 to 0x37 (0x70 to 0x77),
  SLICER LOOP 0x38 to 0x3F (0x78 to 0x7F). Mode buttons CUE LOOP 0x1A (0x5A), SAVED LOOPS 0x1B (0x5B), PITCH PLAY 0x1C (0x5C),
  SLICER LOOP 0x1D (0x5D). Velocity on Bn, aftertouch on An. Not modelled as separate layers.

## Not placed

- FILTER center 9n 0x22 and MAKRO FX center 9n 0x23, KEY LOCK hold 0x25 (shift 0x65), KEY SYNC double press 0x24,
  PHONO 0x13 / LINE 0x14 (decks 3/4), QUANTIZE 0x02 on n 0Ah (hold 2 s), VELOCITY 0x00 on deck channel (hold 2 s).

## Encoders and positions

- Jog, LOOP, FX PARAMETER and TRAX are relative (3Fh / 41h). Jog touch, FX PARAMETER push and TRAX push are note rows
  tagged with the same fig. Pitch fader is pitch bend; `code` is null.
- Hit-target positions are estimated from the product photo; SLIP / KEY LOCK / KEY SYNC order on the side column is assumed.
