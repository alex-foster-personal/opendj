# DDJ-REV1 MIDI Message List (research note)

Recorded Sun 8 Nov 2026 for the DDJ-REV1 practice replica (epic #9366, ADR-0137). Theory-first: no hardware was connected.

## Official sources

| Field | Value |
| --- | --- |
| MIDI list URL | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-REV1/DDJ-REV1_MIDI_Message_List_E1.pdf |
| Filename | `DDJ-REV1_MIDI_Message_List_E1.pdf` (5 pages, 635,727 bytes) |
| sha256 | `f7960b58b0115f2b5db7f55ed57e56226168094895123e67318dd7486a3ba025` |
| Fetch date | 2026-11-08 |
| Panel size | DDJ-REV1 product page, Specifications: Dimensions (W x D x H) 526.0 x 255.5 x 59.2 mm; jog wheel 154 mm |
| Specifications URL | https://www.pioneerdj.com/en/product/controller/ddj-rev1/black/specifications/ |

The vendor PDF and its images are **not** committed to this repository (manufacturer copyright). Only numbers derived from them are.

## Top-panel figure used for the position trace

Page 1 of the MIDI list embeds a labelled top-panel photo as image 2, 2116 x 1033 px,
sha256 `21723a89c12c154338cc5bb98fb18cacc0c02c3c5c45caa9b1dbf578e79c1cd9`. The plate body spans
columns 4..2112 and rows 30..1027 of that image. The round jog platter measures 645 x 643 px and the
square pads 72 x 72 px, so the photo is isotropic: both axes use 526.0 mm / 2109 px = 0.24941 mm/px,
and the visible 998 rows cover 248.9 of the 255.5 mm depth (the front chamfer lies below the image).
The traced pixel boxes live in `tools/deck-diagrams/scripts/build_ddj_rev1_panel.py`; the derived
measurement is `tools/deck-diagrams/devices/ddj-rev1/panel.reference.json`.

## WebMIDI device name

Runtime map selection uses `nameMatch: 'DDJ-REV1'` against the WebMIDI port name. USB identity is not in the PDF and has not been read from a unit.

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev1.ts` exports `DDJ_REV1_MAP`: decks 1 and 2 on channels 1 and 2, pads on channels 8 and 10, FX on 5 and 6, browser and global mixer (including the single SHIFT button and the per-channel FILTER knobs) on channel 7. Deck 3 and 4 rows (DECK SELECT, channels 3 and 4) are hints.
