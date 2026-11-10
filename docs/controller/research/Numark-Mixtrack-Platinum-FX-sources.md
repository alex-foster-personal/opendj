# Numark Mixtrack Platinum FX sources (research note)

Recorded Tue 10 Nov 2026 for issue #10793 (app MIDI map from the manufacturer chart).

## Official sources (clean room)

| Field | Value |
| --- | --- |
| Product page | https://www.numark.com/product/mixtrack-platinum-fx (links: User Guide, Firmware Update v1.10 Mac and Windows, Serato DJ Lite; no MIDI chart or message list) |
| User Guide URL | https://cdn.inmusicbrands.com/Numark/vAC9uYWEnT/mtplfx/MixTrackPlatinumFX-UserGuide-v1.2.pdf |
| Filename | `MixTrackPlatinumFX-UserGuide-v1.2.pdf` (32 pages, created 2019-12-10, modified 2020-09-17) |
| sha256 | `6eac973b498733d77b7707a97d93e632aa1b4fc3bb83ee1caf4922eea229e4d3` |
| Fetch date | Tue 10 Nov 2026 |
| Appendix | Page 31 (English): Technical Specifications and Trademarks & Licenses only |
| MIDI message list | **None published.** Searched the product page (no MIDI download), the User Guide (the only MIDI text is the rear-panel callout "2. USB: Sends USB MIDI data to control various ..." and its translations; no note, CC or channel table), and numark.com, support.numark.com and inmusicbrands.com (no MIDI implementation document for this model, Tue 10 Nov 2026). `docs/controller/research/2026-07-22-map-availability-survey.md` and `(private path)` list Numark as community-only. |

The vendor PDF is **not** committed to this repository (manufacturer copyright).

## Sources this issue excludes

| Source | Why excluded |
| --- | --- |
| Mixxx `Numark Mixtrack Platinum FX.midi.xml` (GPL-2.0-or-later) | Apache 2.0 repo may read Mixxx only to cross-check a wire already taken from a manufacturer chart; no chart exists, so no cross-check basis. |
| `tools/deck-diagrams/devices/numark-mixtrack-platinum-fx/midi.json` | Earlier Mixxx ingest; not a wire source for the app map. |
| Serato DJ Pro quick-start guide (serato.com download 151805) | Serato function table, not a Numark MIDI wire list. |

## WebMIDI device name

No Numark document gives the USB port name, so it is unconfirmed. Candidate strings used in the guard test: "MixTrack Platinum FX", "Numark MixTrack Platinum FX", "Mixtrack Platinum FX MIDI 1". None of the registered `nameMatch` patterns matches those strings; `numark-mixtrack-pro-fx.ts` deliberately misses the Platinum FX model. A connected unit lands in the learn log as unmapped, which is the intended state. No unit was plugged in.

## Outcome

No runtime map, binding, hint, practice fragment or `apps/audio-engine/maps/device-maps.json` entry ships (requirement MIDI-11945). Issue #10793 binding, FX and `nameMatch` acceptance lines stay unmet until a tier-3 hardware capture (`tools/controller-probe/capture.py`) or a future Numark MIDI publication supplies verifiable wires. The `tools/deck-diagrams/scripts/new_panel.py` replica follow-up waits on that map.
