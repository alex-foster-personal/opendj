# Hercules DJ Control MP3 e2 MIDI chart (research note)

Recorded Tue 10 Nov 2026 for issue #10840. Chart-only: no hardware was connected (`hardware_confirmed: false`).

## Official source

| Field | Value |
| --- | --- |
| Document | Hercules `DJ Control MP3 e2` User Manual (EN), section 6 "CONFIGURATION IN MIDI MODE" |
| URL | https://ts.hercules.com/download/sound/manuals/MP3_E2/01_DJ_Control_MP3_e2_ENG.pdf |
| Bytes | 2,698,327 |
| sha256 | `ec5a4a109087082d25a516ff3154fc966f545f5c67a4ae7097726697bca73e4a` |
| Fetch date | 2026-11-10 |
| Input chart | pages 22-25 (notes and Control Change) |
| LED chart | pages 26-29 |

The PDF is not committed (manufacturer copyright). Only numbers taken from it appear in the map.

## Wire convention

- All chart rows use the device's configured MIDI channel; the map uses `ch: 1` throughout.
- Notes: velocity 127 = press, 0 = release.
- Jog CCs 48/49 and pitch CCs 50/51: two's-complement relative increments.
- Analog CCs including EQ and faders: 0..127; EQ 0 = full clockwise per the chart.
- Deck A = Open DJ deck 1; deck B = deck 2.

## WebMIDI nameMatch

`nameMatch: 'DJ\\s*Control\\s*MP3\\s*e2'` (case-insensitive, `regex_lite`-safe). Matches catalog name
"Hercules DJ Control MP3 e2" and compact `DJControl MP3 e2`. Must not match MP3 (non-e2), MP3 LE, or Glow.

## Contract gaps (hints, not omissions)

| Control | Reason |
| --- | --- |
| SHIFTED_K1-K4 (notes 5-8 / 25-28) | No beat-FX or colour-FX `MidiAction` in the runtime contract; follow-up #9788 |
| PITCH CC 50/51 | Relative increments; `deck_pitch` accepts continuous input only |
| Loops/Fx Shift | Latched layer toggle, not a held `shift_modifier` |

`tools/deck-diagrams/devices/hercules-dj-control-mp3-e2/footnotes.md` still says "No official vendor MIDI PDF";
that footnote is stale now that this manual section exists (not edited in the map PR).

## Not verified on hardware

USB product string under the vendor driver, EQ knob direction feel, and WebMIDI enumeration with the
installed control-panel driver (manual 5.2.1/5.2.2).

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-control-mp3-e2.ts` exports `HERCULES_MP3_E2_MAP`
(live, self-registered).
