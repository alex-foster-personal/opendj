# Reloop Beatmix 4 MK2 diagram footnotes

## Source

- Reloop "Beatmix 2/4 MIDI MAP" (shared by Beatmix 2 MK2 and Beatmix 4 MK2):
  https://www.reloop.com/media/custom/upload/Reloop-bm2_4_midi_map.pdf
- Page 1 switches, page 2 jog/encoder and VR tables. Pages 3 and 4 are LED return messages (out of scope).
- PDF kept local only (manufacturer copyright). `source/plate-top.png` is a crop of the top-view product photo
  on https://www.reloop.com/reloop-beatmix-4-mk2 (234969_Reloop_TP.png).

## Channels

- Deck A / B: status 0x91 / 0x92 (0xB1/0xB2, 0xE1/0xE2) = MIDI channel 2 / 3.
- Deck C / D: the vendor DECK-C/DECK-D column, status 0x93 / 0x94 (0xB3/0xB4, 0xE3/0xE4) = MIDI channel 4 / 5,
  same note/CC numbers. Modelled as layer `deck_cd` (and `deck_cd_shift`), active when DECK3 / DECK4 is toggled.
  Rows with "--" in that column (SHIFT, PAD MODE 1/2, FX1 to FX3) have no deck C/D twin.
- DECK3 / DECK4 buttons: note 0x28 in both columns, no shift code.
- Mixer strip n (order on the unit CH3 CH1 | CH2 CH4) uses status 0x9n / 0xBn; strips have no C/D twin.
- Master, phones, cue mix, sampler, crossfader, browser, back: status 0x95 / 0xB5 = MIDI channel 6.

## SHIFT

- +SHIFT rows use different codes on the same channel. Rows marked "--" in the vendor +SHIFT column
  (SHIFT, PAD MODE 1/2, DECK3/4, MASTER, PHONES, pitch fader) have no shift twin.

## Pads

- midi.json holds the Blue pad mode (notes 0x00 to 0x07, shift 0x08 to 0x0F).
- Red mode: notes 0x10 to 0x17, shift 0x18 to 0x1F. Violet mode: pads 1 to 4 = 0x00 to 0x03 (shift 0x08 to 0x0B),
  pads 5 to 8 = 0x10 to 0x13 (shift 0x18 to 0x1B). Not modelled as separate layers.

## Encoders and pitch

- Jog wheel, BEATS and TRAX send relative CC: CW 0x01 upwards, CCW 0x7F downwards. Jog touch, BEATS push and
  TRAX push are note rows tagged with the same fig.
- Pitch fader is 14-bit pitch bend; `code` is null.
- Hit-target positions are estimated from the product photo; FX1 to FX3 order assumed left to right on both decks.
