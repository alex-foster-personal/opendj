# ION Discover DJ MIDI sources (research note)

Recorded Tue 10 Nov 2026 for issue #10849 (SPRINT400-A20). Theory-first: no unit was
connected. Only numbers derived from the vendor documents live in the repository; no vendor
PDF is committed.

## The finding: no published MIDI chart

ION Audio never published a MIDI implementation chart, MIDI message list or MIDI map for the
Discover DJ. Checked Tue 10 Nov 2026:

| Where | What was there |
| --- | --- |
| https://www.ionaudio.com/products/details/discover-dj and `/products/discover-dj` | HTTP 404 (current site) |
| https://web.archive.org/web/20110430201422/http://www.ionaudio.com:80/products/details/discover-dj | Archived product page linking exactly two documents: the Quickstart Guide and a one-page product overview |
| ION downloads directory (Wayback CDX on `ionaudio.com/downloads/*`, filtered to "discover") | `Discover_Drums_USB_-_MIDI_Implementation_Chart_-_v1.0.pdf` for another product; **no** MIDI chart or MIDI map for Discover DJ |
| Quickstart Guide v1.0 | URL `http://www.ionaudio.com/downloads/DISCOVER%20DJ%20Quickstart%20Guide%20-%20v1.0.pdf` (Wayback id capture 20110515200606). 16 pages, 1,412,190 bytes, PDF Title "DISCOVER DJ Quickstart Guide - v1.0", Author ION Audio, created 31 Jul 2009, modified 15 Dec 2009. sha256 `7e43d472db273bac0ecc57770e589470ce28a2843957856037f29fc78f6b05dc`. Getting started (install MixVibes Cross, plug in USB) and a numbered Top Panel Features list in six languages. No MIDI chart, no note, CC or hex numbers, no USB device name |
| Product overview | URL `http://www.ionaudio.com/downloads/discoverdj_2009_ion_product_overview.pdf` (Wayback id capture 20110627192411). 1 page, 173,964 bytes, sha256 `b474a915634725c2a44449d62233401178cb363f6be18b391b23b1a824455603`. Marketing sheet; its only interface line is "Plug-and-play USB connection". No MIDI data |
| Third-party mirrors (manua.ls, manualsdir, manuals.plus, manualmachine) | The same quickstart, or 403; none carries a MIDI chart |

The bundled software was MixVibes Cross, which ships its own controller XML. That XML is a
third-party app mapping, not a manufacturer chart, so it is no source either (see below).

## Sources not used

- Mixxx `Ion Discover DJ.midi.xml` (GPL; the clean-room rule allows it only as a cross-check
  of a chart wire, and there is no chart).
- `tools/deck-diagrams/devices/ion-discover-dj/midi.json` (a Mixxx ingest, not a wire source).
- MixVibes Cross bundled controller XML (third-party app mapping, not a manufacturer publication).
- VirtualDJ and forum byte traces (community reverse engineering).

## Consequence

- No `apps/webui/frontend/src/lib/rb/midi/maps/ion-discover-dj.ts` map, no
  `DEVICE_MAP_ENTRIES` entry, no `apps/audio-engine/maps/device-maps.json` entry, no practice
  fragment, no `panel.json` (`new_panel.py` reads its map from the registry export, so the
  scaffold follow-up is blocked).
- Guard: `apps/webui/frontend/tests/unit/midi-map-ion-discover-dj.test.mjs`.
- Unblocking needs a hardware capture (`tools/controller-probe/capture.py`, the TIER 3 path in
  `(private path)`) or an ION/inMusic publication. The capture
  must also record the USB port name the unit announces, because no ION document gives one,
  and any future `nameMatch` must exclude `ion-discover-dj-pro` (catalog id rank 158, model
  "Ion-Discover-DJ-Pro").
