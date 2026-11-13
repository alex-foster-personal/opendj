# Reloop Beatmix 2 MK2 diagram footnotes

## Source

- Reloop "Beatmix 2/4 MIDI MAP" (shared by Beatmix 2 MK2 and Beatmix 4 MK2):
  https://www.reloop.com/media/custom/upload/Reloop-bm2_4_midi_map.pdf
- Page 1 switches, page 2 jog/encoder and VR (pots, faders) tables. Pages 3 and 4 are LED return messages (out of scope).
- PDF kept local only (manufacturer copyright). `source/plate-top.png` is a crop of the top-view product photo
  on https://www.reloop.com/reloop-beatmix-2-mk2 (234968_Reloop_TP.png).

## Channels

- Left deck and mixer channel 1: status 0x91 / 0xB1 / 0xE1 = MIDI channel 2.
- Right deck and mixer channel 2: status 0x92 / 0xB2 / 0xE2 = MIDI channel 3.
- Master, phones, cue mix, sampler, crossfader, browser, back: status 0x95 / 0xB5 = MIDI channel 6.
- Deck C/D columns (0x93, 0x94), DECK3/DECK4 buttons and channel 3/4 strips belong to the Beatmix 4 MK2 only and are not mapped.

## SHIFT

- +SHIFT rows use different codes on the same channel (`*_shift`, layer shift). Rows marked "--" in the vendor
  +SHIFT column (SHIFT itself, PAD MODE 1/2, MASTER, PHONES, pitch fader) have no shift twin.

## Pads

- midi.json holds the Blue pad mode (notes 0x00 to 0x07, shift 0x08 to 0x0F).
- Red mode: notes 0x10 to 0x17, shift 0x18 to 0x1F. Violet mode: pads 1 to 4 = 0x00 to 0x03 (shift 0x08 to 0x0B),
  pads 5 to 8 = 0x10 to 0x13 (shift 0x18 to 0x1B). Not modelled as separate layers.

## Encoders and pitch

- Jog wheel, BEATS and TRAX send relative CC: CW 0x01 (slow) upwards, CCW 0x7F (slow) downwards.
  Jog touch, BEATS push and TRAX push are note rows tagged with the same fig.
- Pitch fader is 14-bit pitch bend (LSB bit0 to bit6, MSB bit7 to bit13); it has no note/CC number, so `code` is null.
- Hit-target positions are estimated from the product photo; FX1 to FX3 knob order assumed left to right on both units.
