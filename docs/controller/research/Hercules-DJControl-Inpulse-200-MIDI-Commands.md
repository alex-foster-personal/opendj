# Hercules DJControl Inpulse 200 MIDI Commands (research note)

Recorded Tue 10 Nov 2026 for issue #10846 (theory-first map, no unit on hand, `hardware_confirmed: false`).

## Official sources

| Tag | Document | URL | sha256 |
| --- | --- | --- | --- |
| [CHART] | MIDI commands of Hercules DJControl Inpulse 200, V1.0 (Guillemot Corp), sections 1) MIDI INPUT (a Note On, b Control Change) and 2) MIDI OUTPUT | https://ts.hercules.com/download/sound/manuals/DJC_Inpulse200/DJControlInpulse200_MIDI_Commands_V_1_0.pdf | `7be2dd081f87210ec86a80e632748f527b81fe681770a8ccfe020b2c4e6470fb` |
| [MANUAL] | DJControl Inpulse 200 User Manual (EN), sections MODE, LOOP, MASTER and item 9 (dual filters) | https://ts.hercules.com/download/sound/manuals/DJC_Inpulse200/DJControl_Inpulse_200_User_Manual_EN.pdf | `ce0e772fccdb14bb1daadc6a5e39345a93a61260dbf7395a0f5b7e306907eb40` |

Fetch date 2026-11-10. The vendor PDFs are **not** committed to this repository (manufacturer copyright; see `docs/controller/reference/README.md`). Clean room: every wire in the runtime map comes from [CHART]; the Mixxx GPL mapping and `tools/deck-diagrams/devices/hercules-djcontrol-inpulse-200/midi.json` were not used as sources.

## Wire convention

[CHART] prints status bytes `9n` (Note On) and `Bn` (Control Change) with a 0-based channel nibble `n`. The runtime map uses a 1-based channel, so `ch = n + 1`.

| Status nibble | ch | Layer |
| --- | --- | --- |
| `90` / `B0` | 1 | Global (browse, master cue, crossfader, master and headphone levels) |
| `91` / `B1` | 2 | Deck A |
| `92` / `B2` | 3 | Deck B |
| `93` / `B3` | 4 | Global + SHIFT |
| `94` / `B4` | 5 | Deck A + SHIFT (notes, see typos) |
| `B5` | 6 | Deck B + SHIFT CCs |
| `96` / `97` | 7 / 8 | Pads, deck A / deck B |

SHIFT is a hardware layer: the unit moves shifted controls to other channels, so no binding takes `shift: true`. Encoders and jogs send `01..3F` clockwise and `7F..40` counter-clockwise (two's complement), so they are `relative`, not `relativeCentered`. Pitch is a 14-bit pair (MSB CC `08`, LSB CC `28`). EQ is 2-band: LOW CC `02`, HIGH CC `04`, no mid. FILTER CC `01` is the colour FX.

## Chart typos

- Deck A + Shift and Deck B + Shift notes both print status `0x94`. Deck B + Shift would logically be `0x95`, but the chart never prints it, so the map binds nothing on ch 5 notes and never invents `0x95`. Each ch 5 note is a hint naming both readings (for example `94 0C` is SH + LOOP IN long press in the Deck A + Shift section and PFL in the Deck B + Shift section).
- MIDI OUTPUT section iii is titled "Deck A + Shift" but its rows are labelled `_B`.

## nameMatch

The chart's SysEx product name is `DJControl Inpulse 200`. The map's nameMatch matches `Inpulse 200` as a word and rejects the MK2, MK3, 300, 500 and 2000 (a word character after `200` fails every branch; a following word starting `MK` fails). It is written without lookaround because the Rust engine compiles nameMatch with `regex_lite`.

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/hercules-djcontrol-inpulse-200.ts`. Unbound chart rows (MODE3 FX pads, ROLL pads, master and headphone level, ch 5 notes) are hints; the reasons are in the map header. Every binding is INFERRED from [CHART], none is hardware-confirmed.
