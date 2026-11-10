# OMNIS-DUO MIDI Message List (research note)

Recorded Sun 8 Nov 2026 for the deck replica epic (#9366): the AlphaTheta OMNIS-DUO practice
replica and its device map `apps/webui/frontend/src/lib/rb/midi/maps/alphatheta-omnis-duo.ts`.

## Official sources

| Field | Value |
| --- | --- |
| MIDI list URL | https://downloads.support.alphatheta.com/software_info/all-in-one-dj-systems/OMNIS-DUO/OMNIS-DUO_MIDI_Message_List_E1.pdf |
| Filename | `OMNIS-DUO_MIDI_Message_List_E1.pdf` (3 pages, PDF dated Thu 21 Dec 2023) |
| sha256 | `eb4c914d644be58fd3fd91555c1b5a5db027e8c70e534f9a6233ef375cd4b06a` |
| Fetch date | 2026-11-08 |
| Panel size | 500 x 307 x 71 mm (W x D x H), "Maximum dimensions" in the OMNIS-DUO specifications on https://alphatheta.com/en/information/introducing-the-omnis-duo-portable-all-in-one-dj-system |

The older `pioneerdj.com/-/media/.../omnis-duo_midi_message_list_e1.pdf` address now redirects to the
support home page; the AlphaTheta downloads host above serves the file.

The vendor PDF is **not** committed to this repository (manufacturer copyright; see
`docs/controller/reference/README.md`). Nothing from its page 1 top-panel photo is committed either:
`tools/deck-diagrams/devices/omnis-duo/panel.reference.json` holds only the pixel boxes read off it
and the stitched image's sha256 (`b46b62f2be1a556786ee8599652f7111e2de752486ad884009fc08ced8a7d324`,
embedded images 0 and 1 of page 1, each 1571 x 512 px, placed one above the other).

## Channel layout (page 1, "MIDI channel assign")

| Category | Channel |
| --- | --- |
| Browse, DECK1 | 1 |
| DECK2 | 2 |
| DECK3, DECK4 (PC-control deck select only; two physical decks) | 3, 4 |
| EFFECT and MIXER | 5 |
| HOT CUE deck 1 / 2 / 3 / 4 | 6 / 7 / 8 / 9 |
| OTHERS (Bluetooth source) | 12 |

## Rows the map binds (decks 1 and 2)

| Fig | Control | Wire | Action |
| --- | --- | --- | --- |
| 1-7 | PLAY/PAUSE | note 0 (ch deck) | `deck_play_toggle`; LED out note 0 |
| 1-6 | CUE | note 1 | `deck_cue`; LED out note 1 |
| 1-5 | SHIFT | note 63 | `shift_modifier` |
| 1-16 | TEMPO | CC 0 MSB / CC 32 LSB | `deck_pitch` lsbOffset 32 |
| 1-17 | LOAD | note 81 | `browse_load` |
| 1-3 | BEAT LOOP press / + SHIFT (RELOOP) | note 67 / 68 | `deck_auto_loop_toggle` / `deck_loop_exit` |
| 1-23 | SYNC (on screen) | note 31 | `deck_sync_toggle` |
| 1-4 | Jog top / side | CC 34 / 33, centred on 64 | `deck_jog` |
| 1-8..1-15 | HOT CUE A-H | ch 6/7 notes 0-7 | `deck_hot_cue`; LED out same notes |
| 3-1, 3-2, 2-4 | TRIM, EQ HIGH/MID/LOW, COLOR | ch 5 CC 1/6, 2/7, 3/8, 4/9, 5/10 | `mixer_channel` |
| 3-7 | CH FADER | ch 5 CC 17/18 | `mixer_channel` fader |
| 3-3 | CH CUE (HEADPHONES 1/2) | ch 5 CC 70/71, CC button | `channel_cue` |
| 3-6, 3-10 | CROSSFADER, MASTER LEVEL | ch 5 CC 11, 24 | `mixer_global` |
| 3-4, 3-5 | HEADPHONES CUE/MASTER, LEVEL | ch 5 CC 27, 26 | `headphone_mix`, `headphone_level` |
| 4-1 | ROTARY SELECTOR turn / + SHIFT | ch 1 CC 79 / 96, two's complement | `browse_encoder` |
| 3-8 | CH LEVEL METER | MIDI-OUT ch deck CC 2 | meter output |

Every other row on the list is a hint (documented, unbound): BEAT JUMP, BEAT LOOP size turn, jog
touch and + SHIFT rotation, CUE + SHIFT, hot-cue delete, BROWSE press, Beat FX, BOOTH, MIC, MIC ON,
crossfader curve, mixer mode, PC-control mode, DECK SELECT, the on-screen deck rows and the Bluetooth
section. CH CUE and BEAT FX ON/OFF feedback are CC messages, which `LedRule` cannot send.

Not hardware confirmed: no OMNIS-DUO has been connected to this code.
