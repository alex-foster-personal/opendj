# Traktor Kontrol S4 MK3 sources (research note)

Recorded Sun 8 Nov 2026 for the Traktor Kontrol S4 MK3 practice replica (epic #9366, ADR-0137). Theory-first: no hardware was connected.

## Official sources

| Field | Value |
| --- | --- |
| Manual (figure source) | https://www.native-instruments.com/fileadmin/ni_media/downloads/manuals/traktor/TRAKTOR_KONTROL_S4_MK3_Manual_English_0219.pdf |
| Filename | `TRAKTOR_KONTROL_S4_MK3_Manual_English_0219.pdf` (87 pages, 4,525,955 bytes, software version 3.1) |
| sha256 | `a4efebd14a5b8c9f2446799c2d9d1509312bbb52396d1fd888102656549ccaa7` |
| Manual (panel size source) | https://www.native-instruments.com/fileadmin/ni_media/downloads/manuals/traktor/TRAKTOR_KONTROL_S4_MK3_Manual_English.pdf |
| Filename | `TRAKTOR_KONTROL_S4_MK3_Manual_English.pdf` (85 pages, 4,720,680 bytes, software version 3.0) |
| sha256 | `71634a1d733d5f442ac96e62372c1f5e41ee85372630c61c0592aa3b0aaa25a3` |
| Fetch date | 2026-11-08 |
| Panel size | 3.0 manual, section 11 "Specification", Dimensions: Width 21.3 " / 542 mm, Height 2.5 " / 65 mm, Depth 13.3 " / 339 mm |
| MIDI message list | none published. Neither manual has a MIDI chapter; the 3.1 manual's only standalone mode is section 9.5 "Using the S4 as a Standalone DJ Mixer". The unit drives Traktor over HID. |

The vendor PDFs and their images are **not** committed to this repository (manufacturer copyright). Only numbers derived from them are.

## Top-panel figure used for the position trace

Page 17 of the 3.1 manual (section 5 "TRAKTOR KONTROL S4 Overview") embeds a top-panel rendering as
a 2235 x 1502 px PNG, sha256 `226b516e1c0a40f5a0bc4fdc58cba8caefd6fd20430800b7c33e26085c4fa903`. The
plate spans columns 4..2229 and rows 139..1496 of that image. The rendering is isotropic (knob rings
68 x 68 px, CUE and PLAY 112 x 112 px, deck-select keys 48 x 48 px, jog platter 484 x 484 px), so both
axes use 542 mm / 2226 px = 0.24349 mm/px, and the visible 1358 rows cover 330.7 of the 339 mm depth
(the sloped front face with the crossfader assign switches lies outside the top view).

The traced pixel boxes live in `tools/deck-diagrams/scripts/build_traktor_s4_mk3_panel.py`; the
derived measurement is `tools/deck-diagrams/devices/traktor-s4-mk3/panel.reference.json`. Boxes were
read by shape detection on the rendering (edge boxes for buttons, knob rings and encoders; light faces
for pads and deck-select keys; intensity profiles for the jog rim and the fader travels) and checked
on gridded 2x crops. A second read of ten controls with different detectors gave the repeat spread.

## Control inventory

Sections 5.1 Decks (callouts 1..19), 5.1.1 Browse controls (1..5), 5.1.2 Pad section (1..6), 5.2 Mixer
(1..9), 5.2.1 Mixer channel (1..13) and 5.3 FX Unit (1..8) of the 3.1 manual. The `fig` field keeps
those numbers with a section letter (D, B, P, M, C, F). Callouts that are not controls (the deck
display, level meters, input LEDs) are not drawn. 139 controls: 42 per deck side (including that
side's FX unit), 11 per mixer channel (C, A, B, D left to right), 11 mixer globals.

## Wire identities

There are none. `apps/webui/frontend/src/lib/rb/midi/maps/traktor-s4-mk3.ts` exports
`TRAKTOR_S4_MK3_MAP` with no bindings and one hint per control on a synthetic, practice-only
channel 16 (notes for press controls, CCs for continuous ones, numbered in panel order). The map is
registered in `PRACTICE_DEVICE_MAPS` only, never in `DEVICE_MAP_REGISTRY`, so no WebMIDI port and no
engine export ever sees the synthetic keys. Every replica control renders inert with the PARITY-TODO
title. Should Native Instruments publish a MIDI list, the synthetic keys are replaced by the
published ones and the controls bound.
