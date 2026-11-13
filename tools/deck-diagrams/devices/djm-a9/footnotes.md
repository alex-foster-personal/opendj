# DJM-A9 diagram notes

## Provenance

Derived from the [official AlphaTheta MIDI list](https://downloads.support.alphatheta.com/midi-mapping/dj-mixers/DJM-A9/DJM-A9_MIDI_Message_List_E_10.pdf),
`DJM-A9_MIDI_Message_List_E_10.pdf` (version 1.0, six pages), retrieved Fri 13 Nov 2026. SHA-256:
`4bca4d93965d669e8a22bd58a9582ce2fdd23fc3cc29a576203f8e2df819c76f` (the same file recorded in
`docs/controller/research/DJM-A9-MIDI-Message-List.md`). The PDF, its text extraction and any
full-page renders stay local and gitignored. Every Note/CC number in `midi.json` is transcribed from
pages 2 to 6 of that list.

`source/plate-top.png` is the page-one top-panel figure (embedded image, 1383 x 1344 px) cropped to
the plate body, columns 22..1353 and rows 22..1325 (the same rectangle as `panel.reference.json`),
then halved to 666 x 652. Layout percentages come from the traced pixel boxes in
`scripts/build_djm_a9_panel.py` (`_PX`) over that rectangle, so they match `panel.json`.

## Figs

The sheet labels parts L1..L22 (left column), C1..C10 (channel strip and crossfader) and R1..R29
(right column). `checksum.py` accepts only lowercase free-form tags for this scheme, so tags are the
vendor id lowercased: `[l1]`, `[r29]`, `[c10]`. The figure draws C1..C9 once for a representative
strip and the tables repeat them for CH1..CH4, so the four strips use `[c<k>_<ch>]`, for example
`[c2_3]` is C2 TRIM on CH3. One fig can own several targets where the plate has several parts:
C7 (CUE A and CUE B), R24 (LOW, MID, HI) and R26 (eight CH SELECT buttons).

## Channels and encoding

Every status is `9n` (Note) or `Bn` (CC) with n the MIDI channel set in MY SETTINGS; rows use the
factory default, channel 1. Buttons send 00 OFF / 7F ON. Knobs and faders send 00 to 7F. Slide
switches send fixed values (A 00 / THRU 40 / B 7F, ISOLATOR 00 / EQ 7F, RETURN 00 / USB 7F).
Rotary selectors (C1 INPUT SELECTOR, R14 SEND CH, R25 EFFECT SELECT) send OFF for the previous
position and ON for the new one; each position is its own row and the layout joins one representative.
R27 TIME is a 14-bit pair, MSB CC 0D and LSB CC 2D; the layout joins the MSB.
MIDI Timing Clock (F8) is listed under `system_realtime`, not as a control.

## No SHIFT layer

The DJM-A9 has no SHIFT button and the list documents no +SHIFT rows, so `layout.json` declares only
the `base` layer and every `shift_name` is null.

## Sheet anomalies (transcribed as printed)

- CH2 C7 CUE B prints `Bn 11` and C8 CH FADER prints `9n 12` with Trigger/Toggle, where CH1, CH3 and
  CH4 print CUE B as a Note and CH FADER as a CC. `midi.json` keeps the printed values and flags the
  rows; a hardware sniff should adjudicate.
- R18 X-PAD touch and R29 BEAT FX ON/OFF both print `Bn 72`. Both rows are kept as printed.

## Not in the list

MIC LEVEL 1 and 2, +48V, MIDI ON/OFF and START/STOP, CENTER LOCK, level meters and the display have
no MIDI rows and get no targets. The list has no MIDI-OUT column: the mixer accepts no MIDI input,
so there are no LED rows.
