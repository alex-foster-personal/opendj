# Hercules DJ Control MP3 MIDI chart (research note)

Recorded Tue 10 Nov 2026 for issue #10841. Theory-first: no hardware was connected.

## Official source

| Field | Value |
| --- | --- |
| Document title | DJ Control MP3 - User Manual (English) |
| URL | https://ts.hercules.com/download/sound/manuals/DJ_Control_MP3/DJ_Control_MP3_Manual_English.pdf |
| Section | 6 "MIDI STANDARD SUPPORT", pages 14-15, table "MIDI input commands" |
| Size | 451,617 bytes, 18 pages |
| sha256 | `b51c390066cc3b54b697047320fcbcfc2049c39f4e383f2f655ee00454bd1e10` |
| PDF created | 2006-04-27 |
| Fetch date | 2026-11-10 |

No standalone MIDI chart is published for this model; the chart is inside the user manual. The vendor PDF is **not** committed to this repository. Only numbers derived from it are.

The table lists **MIDI input only** (43 Control Change rows on channel 1). There is no MIDI-OUT or LED table.

## WebMIDI port names

| Evidence | Port name |
| --- | --- |
| Windows driver name in the manual ecosystem | Hercules DJ Control MP3 MIDI |
| Linux `amidi -l` (community reports) | Hercules DJ Control MP3 MIDI 1 |

The runtime map uses `nameMatch: 'DJ Control MP3 MIDI'` (plain literal, case-insensitive). That matches the original MP3 ports above and **does not** match "Hercules DJ Control MP3 e2", "... MP3 e2 MIDI", "... MP3 LE MIDI" or Glow, because the token after `MP3 ` is not `MIDI`.

macOS CoreMIDI name is unverified (the manual targets Windows 2000/XP only). USB product IDs from third-party driver databases are not encoded in the map.

## Runtime map disposition

`apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-control-mp3.ts` exports `HERCULES_DJ_CONTROL_MP3_MAP`:

- **Bound (19):** per deck play, cue, sync (Auto Beat Select), PFL, jog, fader, bass/mid/treble EQ; global crossfader. Every button CC uses `ccButton: true` (7F/00).
- **Hints (24):** prev/next track, pitch bend +/-, master tempo, mouse button, four FX controls per deck, pitch knob, ministick X/Y. Browse/load is not bound: the chart has no load control and no relative browse encoder.

### FX gap

Eight FX chart rows (FX Select and FX 1-3 per deck) are learn-log hints. `MidiAction` has no beat-FX or colour-FX action today, so binding them would require a contract change outside this device map. Issue acceptance criterion (d) cannot be met until that contract exists.

### Panel follow-up correction

`tools/deck-diagrams/devices/hercules-dj-control-mp3/footnotes.md` states no vendor PDF exists; that is incorrect: section 6 of the manual above is a tier-1 chart. The Mixxx-derived diagram names `filter_low/mid/high`; the manual names them Bass, Medium and Treble EQ. Those diagram files are left for the separate practice-panel issue.
