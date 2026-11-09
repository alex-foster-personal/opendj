# DJM-A9 MIDI Message List (research note)

Recorded Sun 8 Nov 2026 for the DJM-A9 practice replica (epic #9366, ADR-0137). Theory-first: no unit was connected while this was written.

## Official sources

| Field | Value |
| --- | --- |
| MIDI list URL | https://downloads.support.alphatheta.com/midi-mapping/dj-mixers/DJM-A9/DJM-A9_MIDI_Message_List_E_10.pdf |
| Filename | `DJM-A9_MIDI_Message_List_E_10.pdf` (6 pages, 356,967 bytes, "MIDI Message List", version 1.0) |
| sha256 | `4bca4d93965d669e8a22bd58a9582ce2fdd23fc3cc29a576203f8e2df819c76f` |
| Fetch date | 2026-11-08 |
| Japanese twin | `DJM-A9_MIDI_Message_List_J_10.pdf` at the same path, sha256 `d8f2a9b5f7bd22d0f43629460760c77161e3539837e93d5992fccbb3c334c561`; it prints the same rows, including the CH2 anomaly below |
| Panel size | DJM-A9 product page: Dimensions (W x D x H) 407.4 x 458.3 x 107.9 mm |
| Product page URL | https://www.pioneerdj.com/en/product/dj-mixers/djm-a9/ (the Specifications comparison table) |
| MIDI direction | AlphaTheta support article "DJM-A9 MIDI-compatible software" (https://support.alphatheta.com/en-US/articles/16006872025369) points software at this list; the sheet has no MIDI-OUT column, and the companion article on MIDI connections says the mixer does not accept MIDI input |

The vendor PDFs and their images are **not** committed to this repository (manufacturer copyright). Only numbers derived from them are.

## Top-panel figure used for the position trace

Page 1 of the MIDI list embeds a labelled top-panel figure as image 1, 1383 x 1344 px (JPEG),
sha256 `205f2c2c4afe3d03814022b11e404cdf23af03804554d18de4ea606eaf5ec273` of the embedded JPEG
bytes (`pdfimages -j`). The plate body spans columns 22..1353 and rows 22..1325 of that image.
The round BEAT FX ON/OFF button measures 59 x 58 px, the LEVEL/DEPTH cap 50 x 50 px and the TIME
ring 52 x 52 px, so the figure is isotropic: both axes use 407.4 mm / 1332 px = 0.30586 mm/px,
and the visible 1304 rows cover 398.8 of the 458.3 mm depth (the sloped rear connector panel lies
behind the top plate and shows only as the strip above row 22). The traced pixel boxes live in
`tools/deck-diagrams/scripts/build_djm_a9_panel.py`; the derived measurement is
`tools/deck-diagrams/devices/djm-a9/panel.reference.json`.

## Sheet anomalies (hardware adjudication wanted)

- CH2 rows C7/C8 print CUE B as `Bn 11` and CH FADER as `9n 12 Trigger/Toggle`. CH1, CH3 and CH4
  print CUE B as a note (0x10, 0x12, 0x13) and CH FADER as a CC (0x11, 0x13, 0x14), and CH1's fader
  is already CC 0x11, so the map reads CH2 as CUE B note 0x11 and CH FADER CC 0x12. One sweep of
  the CH2 fader through `tools/controller-probe/capture.py sniff` settles it.
- R18 X-PAD touch and R29 BEAT FX ON/OFF both print `Bn 72`. The map carries one hint on CC 0x72
  naming both.
- Every status is `9n` / `Bn` with "n: value of channel No." (UTILITY > MIDI CH). The map binds
  the factory default, channel 1 (`A9_MIDI_CH`).

## WebMIDI device name

Runtime map selection uses `nameMatch: 'DJM-A9'` against the WebMIDI port name. USB identity is not in the PDF and has not been read from a unit.

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/djm-a9.ts` exports `DJM_A9_MAP`: 33 bindings (per
channel TRIM, HI, MID, LOW, COLOR as the channel filter, CH FADER and CUE A as channel cue, plus
CROSSFADER, MASTER LEVEL, HEADPHONES A MIX and LEVEL, MASTER CUE A), no LED rules, and 117 hints
for every other row the sheet documents (microphone section, Sound Color FX, HEADPHONES B, BOOTH,
curve switches, MULTI I/O, BEAT FX, input selectors, CUE B, CROSSFADER ASSIGN).
