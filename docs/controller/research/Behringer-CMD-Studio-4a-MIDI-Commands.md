# Behringer CMD Studio 4a MIDI (research note)

Live map: `apps/webui/frontend/src/lib/rb/midi/maps/behringer-cmdstudio4a.ts`
(`BEHRINGER_CMD_STUDIO_4A_MAP`, `hardware_confirmed: false`).

## Source

| Kind | Document |
| --- | --- |
| [CHART] | Behringer "CMD STUDIO 4a MIDI MAP", one page. Original `http://www.behringer.com/assets/CMD_STUDIO_4A_MAP.pdf` (dead). Archive: `https://web.archive.org/web/20150701200912id_/http://www.behringer.com:80/assets/CMD_STUDIO_4A_MAP.pdf` sha256 `6e7df9a2224bef2ebae98d5926f353ce4428721dd0325bc32599eefa6c2f70fd` |

## Wire convention

- Two physical sides, four deck channels: left deck A = ch 1, deck C = ch 3 (same ids); right deck B = ch 2, deck D = ch 4.
- Grey strip (browse, master, crossfader, headphones) stays on ch 1 when the deck select switches sides.
- Scientific pitch: MIDI note = 12 * (octave + 1) + pitch class; `ch` in the map is 1-based.
- Grey silkscreen labels on the archived chart are mostly illegible; roles follow bold wire labels and layout.

## Notable wires

- Pitch faders: MIDI Pitch Bend (not CC pairs). PITCH BEND +/- buttons are separate note inputs (hinted).
- FX knob nearest the mixer: colour FX filter (`mixer_channel` target `filter`); other FX controls hinted (no beat-FX `MidiAction`).
- Peak meters CC126/CC127: chart range 48-63, left unbound (`MidiMeterOutput` has no offset).
- Jog/browse encoders: `relativeCentered` (encoding unverified until hardware).

## USB name match

`nameMatch`: `\bCMD\s*Studio\s*4a\b` (case-insensitive). Matches `CMD Studio 4a`, `CMD STUDIO 4a`, `CMDStudio4a`, `Behringer CMDStudio4a`; rejects other CMD models.

## Practice diagram footnote

The deck-diagram `midi.json` footnote claiming no official vendor PDF is superseded by the [CHART] above; that GPL file was not used as a wire source.
