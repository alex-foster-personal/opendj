# DDJ-FLX4 MIDI Message List (research note)

Recorded Sat 12 Sep 2026 for issue #1855 (theory-first map). Hardware adjudication round 1 ran on the Air, Fri 18 Sep 2026 (see below).

## Official source

| Field | Value |
| --- | --- |
| URL | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX4/DDJ-FLX4_MIDI_message_List_E1.pdf |
| Filename | `DDJ-FLX4_MIDI_message_List_E1.pdf` |
| sha256 | `e03ac376ebaa2d461c1a2c832c3c182e3579c893a1531944ebac1b5ce620388f` |
| Fetch date | 2026-09-12 |

The vendor PDF is **not** committed to this repository (manufacturer copyright; see `docs/controller/reference/README.md`).

## WebMIDI device name

Runtime map selection uses `nameMatch: 'DDJ-FLX4'` against the WebMIDI port name. macOS Audio MIDI Setup shows `[DDJ-FLX4]`; Bluetooth aliases `DDJ-FLX4_1` through `DDJ-FLX4_16` still contain that substring.

USB identity, read live from a plugged-in DDJ-FLX4 on the Air, Fri 18 Sep 2026 (`ioreg -p IOUSB -l`):

| Field | Value |
| --- | --- |
| idVendor | `0x2b73` (11123, AlphaTheta Corporation) |
| idProduct | `0x0045` (69) |
| bcdDevice | `0x0105` (261, firmware 1.05) |
| USB Product Name | `DDJ-FLX4` |
| CoreMIDI | 1 entity, 1 source + 1 destination, driver `com.apple.AppleMIDIUSBDriver` |
| USB audio | 2 in / 4 out, 48 kHz |
| Universal Device Inquiry (`F0 7E 7F 06 01 F7`) | no reply within 1.5 s |

The PDF re-fetched the same day matched the recorded sha256.

## Expanded expected map

`tools/controller-probe/flx4_expected_map.json` expands every per-deck and per-pad row from the PDF into one control per physical wire.

**control_count: 402**

## Runtime map

`apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts` binds the DDJ-400 P0 analog plus CFX (filter) on deck channels. See the map-availability survey row dated 2026-09-12.

## Hardware adjudication, round 1 (Fri 18 Sep 2026, the Air)

Instrument: `uv run tools/controller-probe/capture.py sniff` (CTRL-02) against the live unit, the maintainer at the deck sweeping roughly half the surface. Evidence: `tools/controller-probe/captures/flx4_observed_wires_2026-09-18.json` (44 distinct wires, 3566 messages).

| Result | Count | Detail |
| --- | --- | --- |
| Wires seen that match the vendor sheet exactly | 42 | jog (touch, vinyl on, wheel side), tempo MSB+LSB, pad-mode buttons, sampler pads 1-8, trim/EQ/fader MSB+LSB both decks, crossfader, CH CUE both decks, headphone mix/level, mic level, master level, SMART CFX |
| Wires seen that contradicted OUR transcription | 2 | CFX knob deck 1 sent `B6 17` / `B6 37` (channel 7). The PDF row 3-5 says channel 7 too; the Sat 12 Sep expected map and runtime map had transcribed it as channel 1/2. Fixed in both, plus the deck 2 twin by the same row. |
| Wires seen that contradicted the vendor sheet | 0 | |
| Runtime P0 bindings hardware-confirmed | 13 of 65 | tempo, trim, EQ x3, fader (both decks where swept), crossfader, master, CH CUE x2 |
| Runtime P0 bindings still inferred from the PDF only | 52 of 65 | play, cue, shift, 4 BEAT/exit, hot cue pads, beat loop pads, browse, load, master cue, deck 2 CFX, deck 2 knobs not swept |

Coverage of the 402-control expected map by section: deck 5/52, mixer 25/55 (27 after the CFX correction), performance 12/272, effect 0/15, browse 0/8. Everything not listed as seen is INFERRED from the vendor PDF, not measured. Round 2 target: transport, hot cue and beat loop pads, browse/load, deck 2, with `capture` (guided) instead of `sniff` so each wire carries a verdict.
