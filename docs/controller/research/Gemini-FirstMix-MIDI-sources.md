# Gemini FirstMix MIDI sources (research note)

Recorded Tue 10 Nov 2026 for issue #10830 (SPRINT400-S16). Theory-first: no unit was
connected. No vendor PDF is committed.

## The finding: no published MIDI chart

Gemini never published a MIDI implementation chart, MIDI message list or MIDI map for the
FirstMix family (base FirstMix, FirstMix I/O, FirstMix Pro). Checked Tue 10 Nov 2026:

| Where | What was there |
| --- | --- |
| https://www.geminisound.com/ | HTTP 200 (Shopify storefront). Site search and product URLs for "FirstMix" were not exhaustively crawled behind the storefront; no MIDI chart or MIDI message list surfaced in public search results for "Gemini FirstMix MIDI implementation" or "Gemini FirstMix MIDI map" |
| Retailer product pages (B&H, PlanetDJ, DJKit) | Discontinued listings with panel photos and 2-band EQ in specs for the base FirstMix; no MIDI appendix or download link |
| Operating Instructions mirrors | `https://musicalstore2005.com/strumenti_accessori/firstmix_en.pdf` (base FirstMix) and `https://shop.asl.ch/.../first_mix_pro.pdf` (FirstMix Pro) return HTTP 403 (Cloudflare challenge) from this lane; no PDF bytes were obtained, so no sha256 is recorded here |
| Gemini support / downloads (manual search) | No published MIDI implementation chart, note/CC table or hex wire list for FirstMix, FirstMix I/O or FirstMix Pro |

The surviving vendor documents are Operating Instructions only: control names, setup steps and
specifications, with no MIDI channel, note, CC or SysEx table.

## Sources not used

- Mixxx `Gemini FirstMix.midi.xml` (GPL-2.0-or-later; clean-room rule allows cross-check of a
  chart wire only, and there is no chart).
- `tools/deck-diagrams/devices/gemini-firstmix/midi.json` (Mixxx ingest; header
  `"mixxx_gpl": true`, not a wire source for the app map).
- VirtualDJ and forum byte traces (community reverse engineering).

## Catalog record

`tools/deck-diagrams/catalog/controllers.json` lists `gemini-firstmix` at popularity rank 136,
`doc_tier: 2`, `status.docs: "mixxx"`, `popularity_notes: "Mixxx community map ingest"`; no
vendor MIDI document.

Unresolved discrepancy: retailer specs for the base FirstMix describe **2-band EQ**, while the
Mixxx-derived `tools/deck-diagrams/devices/gemini-firstmix/layout.json` carries three EQ knobs
per deck (high/mid/low). Neither the blocked manual fetch nor hardware in this lane resolves
which is correct; do not assert either.

## Consequence

- No `apps/webui/frontend/src/lib/rb/midi/maps/gemini-firstmix.ts`, no `DEVICE_MAP_ENTRIES`
  entry, no `apps/audio-engine/maps/device-maps.json` entry, no practice fragment, no
  `tools/deck-diagrams/devices/gemini-firstmix/panel.json` (follow-up issue #10915).
- Guard: `apps/webui/frontend/tests/unit/midi-map-gemini-firstmix.test.mjs`.
- Unblocking needs a hardware capture (`tools/controller-probe/capture.py`, tier 3 in
  `(private path)`) or a Gemini publication of a MIDI chart or
  message list. The capture must record the USB port name the unit announces (no Gemini
  document in this search gives one). A future `nameMatch` must not swallow unrelated Gemini
  catalog ids (`gemini-gmx`, `gemini-g4v`, `gemini-cdmp-7000-l-audio`,
  `gemini-cdmp-7000-r-audio`) or FirstMix Pro / FirstMix I/O unless wire identity is proven
  identical.
