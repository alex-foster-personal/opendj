# Inpulse 500 diagram footnotes

Ingested from Mixxx controller mapping (GPL-2.0-or-later).

## Source

- Mixxx file: `Hercules_DJControl_Inpulse_500.midi.xml`
- Upstream: https://github.com/mixxxdj/mixxx/blob/main/res/controllers/Hercules_DJControl_Inpulse_500.midi.xml
- Local archive: `tools/deck-diagrams/devices/hercules-inpulse-500/source/Hercules_DJControl_Inpulse_500.midi.xml`

## Status

- 328 controls parsed from Mixxx `<control>` status/midino entries
- Positions are an auto grid only - **spot-check required** against hardware or photos
- Official vendor MIDI chart: https://ts.hercules.com/download/sound/manuals/DJC_Inpulse500/DJControlInpulse500_MIDI_Commands.pdf (used by the Open DJ live app map in apps/webui/frontend/src/lib/rb/midi/maps/hercules-inpulse-500.ts)
- SHIFT twins inferred only when Mixxx names end with `_shift`
- Script-bound and HID-only surfaces may be incomplete vs real hardware

## License note

Mixxx mappings are GPL. This diagram consumes MIDI numbers for teaching/reference;
retain GPL notice if redistributing derived mapping work.
