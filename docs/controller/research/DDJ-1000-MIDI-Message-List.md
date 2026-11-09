# DDJ-1000 MIDI Message List (research note)

Recorded Sun 8 Nov 2026 for the DDJ-1000 practice replica (epic #9366, checklist
`(private path)`). Theory only: no DDJ-1000 has been on a probe rig yet.

## Official sources

| Field | Value |
| --- | --- |
| MIDI list URL | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-1000/DDJ-1000_MIDI_Message_List_E1.pdf |
| Filename | `DDJ-1000_MIDI_Message_List_E1.pdf` ("List of MIDI message version 1.00", 8 pages, A4, Dec 2017) |
| sha256 | `28d68d514774daa81e603d9a8709105283f38fbc944650671971ecf57cb95d11` |
| Page 1 figure | top-panel artwork, a raster sliced into 1 px strips (no single embedded image); the page rendered with `pdftoppm -r 600` is 4961 x 7017 px, sha256 `105b39b792808cacc8e84dc4af70eb90ef5bcb3c96e20935a9069e9062622fb8` |
| Panel size URL | https://downloads.support.alphatheta.com/manuals/dj-controllers/DDJ-1000/DDJ-1000_DRH1656B_quickstart-manual.pdf |
| Panel size | 708 mm (width) x 73.4 mm (height) x 361.4 mm (depth), Quick Start Guide DRH1656B page 15, Specifications; sha256 `0c1dcc4324add99c23d46dc7cc7623c3844ed267b1682e874bdef6e22272119e` |
| Fetch date | 2026-11-08 |

The vendor PDFs and the figure are **not** committed to this repository (manufacturer copyright;
see `docs/controller/reference/README.md`). Only numbers read off them are.

## Channel table ([PDF] MIDI channel assignment)

| Category | Channel |
| --- | --- |
| DECK 1 / 2 / 3 / 4 (other than performance pads) | 1 / 2 / 3 / 4 |
| EFFECT | 5 |
| (no use) | 6 |
| BROWSER + global mixer (SHIFT is per deck channel, [PDF] D19) | 7 |
| Performance pads deck 1 / 2 / 3 / 4, unshifted | 8 / 10 / 12 / 14 |
| Performance pads deck 1 / 2 / 3 / 4, shifted | 9 / 11 / 13 / 15 |

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/ddj-1000.ts` (`DDJ1000_MAP`) binds all four decks the
way the FLX10 map does (the left deck of the replica carries deck 1, the right deck 2; DECK SELECT
reaches 3 and 4 on the hardware): transport, 14-bit TEMPO, 4 BEAT LOOP/EXIT and its shifted exit
row, BEAT SYNC, KEY SYNC, the jog (top vinyl on/off and side, relative centred on 64), the HOT CUE
pad-mode button, eight hot-cue pads and eight beat-loop pads per deck, trim / EQ / fader / COLOR FX
knob / headphone cue per channel strip, browse / LOAD 1-4 / crossfader / master / headphone mix
and level / MASTER CUE, and SHIFT on every deck channel. Everything else the sheet
lists is a hint. The panel layout `tools/deck-diagrams/devices/ddj-1000/panel.json` is built by
`tools/deck-diagrams/scripts/build_ddj1000_panel.py` from pixel boxes traced on the page-1
figure; `panel.reference.json` beside it is the measurement of record.

## Sheet quirks worth knowing

- SHIFT ([PDF] D19) is a per-deck button on the deck channel (note 63), not a channel-7 wire.
- DECK SELECT ([PDF] D6) sends note 114 on the channel of the layer being switched to (3 on the
  left deck, 4 on the right); the printed table shows both 1-to-3 and 3-to-1 rows with status 92.
- The COLOR FX knobs ([PDF] F1-1..F1-4) are channel-7 CCs 23..26, one per channel strip; the
  COLOR FX select buttons ([PDF] F2..F5) are channel-7 notes 0..3.
- The Beat FX section ([PDF] F6..F11) is channel 5; FX SELECT and CH SELECT are rotary switches
  that send one note per position.
- The figure is about 8 percent squashed vertically relative to the published width x depth, so the
  trace calibrates x and y separately (the 206 mm jog then measures 215 x 217 mm, outer ring).
