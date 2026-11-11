# DDJ-REV5 MIDI Message List (research note)

Recorded Sun 8 Nov 2026 for the DDJ-REV5 practice replica (epic #9366, checklist
`(private path)`). Theory only: no DDJ-REV5 has been on a probe rig yet.

## Official sources

| Field | Value |
| --- | --- |
| URL | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-REV5/DDJ-REV5_MIDI_message_List_E1.pdf |
| Filename | `DDJ-REV5_MIDI_message_List_E1.pdf` (9 pages, A4, Jul 2023) |
| sha256 | `577af3726e3e97fcaee25dc104a4e4ef42d418f6808f46bfb3eb284a17b67791` |
| Page 1 image 0 | top-panel figure, 1852 x 1038 px, sha256 `8790b9a4924f68cbdc70675cc4fe0da7521a7d6ae5f79a29b9d7b80e8647724f` |
| Fetch date | 2026-11-08 |
| Panel size | 736.0 x 377.2 x 73.4 mm (W x D x H), pioneerdj.com DDJ-REV5 specifications |

The vendor PDF and its figure are **not** committed to this repository (manufacturer copyright;
see `docs/controller/reference/README.md`). Only numbers read off them are.

## Channel table ([PDF] MIDI channel assignment)

| Category | Channel |
| --- | --- |
| DECK 1 / 2 / 3 / 4 | 1 / 2 / 3 / 4 |
| FX (CH1/CH3) / FX (CH2/CH4) | 5 / 6 |
| BROWSER, global section (incl. SHIFT, FILTER, LOAD) | 7 |
| Performance pads deck 1 / 2, unshifted | 8 / 10 |
| Performance pads deck 1 / 2, shifted | 9 / 11 |

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev5.ts` (`REV5_MAP`) binds decks 1 and 2 only:
transport, 14-bit TEMPO, SYNC, AUTO LOOP and the loop halve/double pair, KEY -/+ and KEY SYNC,
the jog (top and side, relative centred on 64), HOT CUE pad mode, eight hot-cue pads per deck,
trim / EQ / fader / FILTER / headphone cue per channel, browse / LOAD / crossfader / master /
headphone mix and level, and the single channel-7 SHIFT. Everything else the sheet lists is a
hint. The panel layout `tools/deck-diagrams/devices/ddj-rev5/panel.json` is built by
`tools/deck-diagrams/scripts/build_ddj_rev5_panel.py` from pixel boxes traced on the page-1
figure; `panel.reference.json` beside it is the measurement of record.

## Sheet quirks worth knowing

- SHIFT is a global button on channel 7 note 63 ([PDF] E12, printed as "M12" in the table).
- BPM TRANSITION SELECT ([PDF] D9) sends channel 7 note 62 from either deck; its shifted row
  (note 48) is the same wire as FX1-1 + SHIFT.
- HEADPHONES CUE CH1 / CH2 are deck-channel note 7, not a channel-7 wire.
- DECK 1/3 and DECK 2/4 send note 6 on the layer's own channel (3 / 4) plus deck on/off
  notes 62 on channels 1..4.
