# DDJ-SB MIDI Message List (research note)

Recorded Tue 10 Nov 2026 for the DDJ-SB practice replica (issue #10858, ADR-0137). Theory-first: no hardware was connected.

## Official sources

| Field | Value |
| --- | --- |
| MIDI list URL | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-SB/DDJ-SB_List_of_MIDI_Messages_E.pdf |
| Filename | `DDJ-SB_List_of_MIDI_Messages_E.pdf` (2 pages, 1,194,967 bytes) |
| sha256 | `2a8e39d6f5e46ed31e1a9ec93b443dabad244a2060f2f751d336120c481d8107` |
| Fetch date | 2026-11-10 |
| Panel size | Pioneer DJ DDJ-SB archived specifications: Dimensions (W x D x H) 487.0 x 271.2 x 58.5 mm; jog wheel 128 mm |
| Specifications URL | https://www.pioneerdj.com/en/product/controller/archive/ddj-sb/black/specifications/ |

The vendor PDF and its images are **not** committed to this repository (manufacturer copyright). Only numbers derived from them are.

## Top-panel figure used for the position trace

Page 1 of the MIDI list embeds a labelled top-panel drawing as two stacked 1424 x 400 px plates. Abutted they form a 1424 x 800 px plate,
sha256 `9984f73e30ae7de5e6100033aeeaf2d5358af30b0a178a57b181d957d6480088`. The chassis spans
columns 6..1415 and rows 5..789 of that image (1410 x 785 px). The round jog platter measures 371 x 371 px
(128.14 mm at the plate x-scale, against the 128 mm spec) and the square pads 79 x 79 px, so the drawing is
isotropic: both axes use 487.0 mm / 1410 px = 0.34539 mm/px, and the visible 785 rows cover 271.13 of the
271.2 mm depth. The traced pixel boxes live in `tools/deck-diagrams/scripts/build_ddj_sb_panel.py`; the derived
measurement is `tools/deck-diagrams/devices/ddj-sb/panel.reference.json`.

## WebMIDI device name

Runtime map selection uses `nameMatch: 'DDJ-SB(?:$|[^0-9])'` against the WebMIDI port name so `DDJ-SB`,
`Pioneer DJ DDJ-SB MIDI 1` and colour suffixes (`DDJ-SB-S`) match, while `DDJ-SB2` and `DDJ-SB3` do not.
USB identity is not in the PDF and has not been read from a unit.

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/ddj-sb.ts` exports `DDJ_SB_MAP`: decks 1 and 2 on channels 1 and 2,
pads on channels 8 and 9 (the sheet's p=7 / p=8, not the later Pioneer 8/10 pattern), FX on 5 and 6,
browser and global mixer on channel 7. MASTER LEVEL, HEADPHONES VOL and FILTER FADE send no MIDI
(the sheet prints no row, or "LED are lit by DDJ-SB"); those three panel controls join through
channel-16 placeholder hints so the replica can draw them inert.

Two decks only. Four performance pads per deck (HOT CUE slots A-D). Per-deck SHIFT is note 0x3F on
the deck channel.

## Channel table ([PDF] p.1)

| UI | MIDI channel | Hex nibble |
| --- | --- | --- |
| DECK 1 / 2 (other than pads) | 1 / 2 | n=0 / n=1 |
| EFFECT FX1 / FX2 | 5 / 6 | m=4 / m=5 |
| BROWSER + global mixer | 7 | m=6 |
| PERFORMANCE PADS DECK 1 / 2 | 8 / 9 | p=7 / p=8 |
| MIDI-OUT communication | 12 | m=B |

## Fig ids ([PDF] table)

PLAY 1, CUE 2, SYNC 3, JOG 4, TEMPO 5, VINYL 6, KEYLOCK 7, SHIFT 8, CROSSFADER 9, CH FADER 10,
EQ HI/MID/LOW 11/12/13, FILTER 14, CH CUE 15, MASTER LEVEL 16, HEADPHONES MIX 17, HEADPHONES VOL 18,
FILTER FADE 19, FX1-1 ON 20, FX2-1 ON 21, FX1-2 ON 22, FX1 knobs/BEATS 23, FX2-2 ON 24, FX1-3 ON 25,
FX2-3 ON 26, FX2 knobs and BROWSE both 27 (Pioneer reused 27), LOAD 28/29, BACK 30, pads 31-34,
modes 35-38.
