# DDJ-FLX4 MIDI Message List (research note)

Recorded Sat 12 Sep 2026 for issue #1855 (theory-first map; hardware adjudication pending Jake's boat, week commencing Mon 14 Sep 2026).

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
