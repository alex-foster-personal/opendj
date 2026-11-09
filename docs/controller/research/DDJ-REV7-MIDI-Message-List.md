# DDJ-REV7 MIDI Message List (research note)

Recorded Sun 8 Nov 2026 for the DDJ-REV7 practice replica (epic #9366, deck-replica checklist `(private path)`).

## Official sources

| Field | Value |
| --- | --- |
| MIDI message list URL | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-REV7/DDJ-REV7_MIDI_message_List_E1.pdf |
| Filename | `DDJ-REV7_MIDI_message_List_E1.pdf` (7 pages, A3, "(c) 2022 AlphaTheta Corporation") |
| sha256 | `22b9306a15704ef0938edd6f4278be4d1823ca492e5aa10d7f6ab0e82ab0b50a` |
| Fetch date | Sun 8 Nov 2026 |
| Panel size | 732.0 mm W x 382.2 mm D x 82.4 mm H, 10.7 kg: pioneerdj.com DDJ-REV7 product page, Specifications tab (https://www.pioneerdj.com/en/product/controller/ddj-rev7/black/specifications/), read Sun 8 Nov 2026 |

The vendor PDF and its images are **not** committed to this repository (manufacturer copyright; see `docs/controller/reference/README.md`). Only numbers derived from them are.

## Top-panel figure used for the measured trace

Page 1 of the PDF carries a top-down photo of the controller with the fig labels (B1-B3, D1-D26, M1-M16, E1-E13, P1-P14) drawn over it as vector text; M17-M33 label the rear and front panel images below it and are not top-panel controls.

| Field | Value |
| --- | --- |
| Embedded image | page 1, image index 1 (pdfimages numbering), 2500 x 1400 px RGB JPEG |
| Raw JPEG sha256 | `866ebff74fe032891f654214b546af99fd718db32ee0f9d9ae238ac615ee37d5` |
| Placement on the page | x 46.92..745.34 pt, y 196.74..573.51 pt (A3 page 841.68 x 1190.40 pt) |
| Body box in image px | x 100..2399, y 117..1298 (pixels darker than 200 of 255 against the white page) |
| Calibration | 732.0 mm / 2300 px = 0.31826 mm/px in x; 382.2 mm / 1182 px = 0.32335 mm/px in y |

The fig labels' page positions were read with PyMuPDF (`page.get_text("words")`) and projected into image pixels through the placement rectangle; they sit just above and to the left of the control they name, and were used only to identify which control is which. Control bodies were traced on the photo itself (`tools/deck-diagrams/devices/ddj-rev7/panel.reference.json`, `trace.method`).

## MIDI channel assignment (page 1 table)

| Channel | Category |
| --- | --- |
| 1 / 2 | DECK 1 / DECK 2 (other than PERFORMANCE PAD and SLOT/PAD) |
| 3 / 4 | DECK 1 / DECK 2 SLOT/PAD mode buttons (D19-D22) |
| 5 / 6 | FX (CH1) / FX (CH2) |
| 7 | BROWSER, global mixer, SHIFT, beat FX section |
| 8 / 10 | DECK 1 / DECK 2 PERFORMANCE PAD |
| 9, 11-15 | not used |
| 16 | MIDI-OUT communication |

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev7.ts` (`REV7_MAP`, `nameMatch: 'DDJ-REV7'`) binds the P0 analog of the DDJ-400 and FLX4 maps that the REV7's controls support: START/STOP, TEMPO (14-bit), JOG (relative, centred on 64), AUTO LOOP, LOOP 1/2X and 2X, SYNC, KEY -/+, TRIM, ISO HI/MID/LOW, FILTER, CH FADER, HOT CUE pads, BROWSE, LOAD, MASTER LEVEL, CROSSFADER, HEADPHONES MIX and LEVEL, SHIFT. The REV7 has no CUE, CH CUE or MASTER CUE button, so `deck_cue`, `channel_cue` and `master_cue` are not bound. Every other documented top-panel row, including each control's +SHIFT row, is a hint, never an invented binding.

No hardware adjudication has been run: every wire in the map is transcribed from the PDF and none is hardware-confirmed yet.
