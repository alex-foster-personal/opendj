# DJ TechTools Midifighter sources (research note)

Recorded Tue 10 Nov 2026 for issue #10820 (SPRINT400-S13, `djtechtools-midi-fighter`).

## Official sources (clean room, tier 1)

DJ TechTools published the original kit-set Midifighter firmware as open source on SourceForge (Robin Green / DJ Tech Tools 2010). The README is the manufacturer's MIDI implementation chart. **No firmware file is committed** (GPL; repo is Apache 2.0). Only facts (numbers) are transcribed.

| Source | URL | sha256 (fetched Tue 10 Nov 2026, SVN HEAD r13) |
| --- | --- | --- |
| [MF] README.txt | https://sourceforge.net/p/midifighter/code/HEAD/tree/README.txt?format=raw | `90575de158f8520366c05d18e20ab0a0b7eaff4d2a51edd8e96a600642a15ed7` |
| [USB] usb_descriptors.c | https://sourceforge.net/p/midifighter/code/HEAD/tree/usb_descriptors.c?format=raw | `0d80a60655088efcc783605510fa319636ff5963c44fce6870d9e5ce160f6edc` |
| [EE] eeprom.c | https://sourceforge.net/p/midifighter/code/HEAD/tree/eeprom.c?format=raw | `4a93de15603bbe76411e760507d472092511918431e4c2e3aed0d483a0d6c468` |
| [FW] midifighter.c | https://sourceforge.net/p/midifighter/code/HEAD/tree/midifighter.c?format=raw | `353c7d189973af53c8f076e4667db98b1bd235c9e2c584f469150cc76cc9f857` |

Cross-check only (not a wire source): DJTT forum thread on Menu Mode defaults (channel 3, base note 36).

## Facts from the chart

- Default MIDI channel 3 ([EE] `EE_MIDI_CHANNEL 2` = channel 3).
- 16 keys, notes 36-51 in a 4x4 grid (base note 36 bottom-left).
- LED feedback: Note On on the same channel, velocity 0 = off ([MF] / [FW]).
- Expansion digital inputs: notes basenote+16..+19 (52-55), factory disabled.
- Expansion analog A1-A4: CC 16-19 full range, CC 20-23 upper half ([MF] / [FW]).
- USB `ProductString` = `Midifighter` ([USB]).

## WebMIDI device name

Plain `nameMatch: 'Midifighter'` matches the USB product string. Later models enumerate as `Midi Fighter ...` (with a space) and must not match; regex_lite in the Rust engine has no lookaround.

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/djtechtools-midi-fighter.ts` exports `DJTT_MIDI_FIGHTER_MAP` with `live: true`. Grid keys drive two decks; expansion CC 16-19 carry mixer faders and crossfader/master (operator-chosen assignment for DIY wired faders).

**hardware_confirmed: false** - no unit was plugged in.

**Scope:** Normal mode factory defaults only. No jog, encoder or FX hardware exists on this device; none is bound. Jog acceptance criterion in the issue cannot be met without a wire outside the chart.
