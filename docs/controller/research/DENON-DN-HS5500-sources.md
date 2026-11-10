# Denon DJ DN-HS5500 - sources

Clean-room record for catalog id `denon-dn-hs5500` (popularity rank 127). The DN-HS5500 is a
single-deck media player with a dual-layer mode. Issue #10821 asked for an Open DJ app MIDI map
sourced only from the manufacturer's published chart; this note records why that map does not ship.

## MIDI: no published message list

Denon DJ publishes **no MIDI Command List, implementation chart or message list** for the
DN-HS5500.

Evidence:

- On `https://www.denondj.com/downloads` (legacy products, DN-HS5500 row), the only downloads are
  Firmware, ASIO Driver v2.22, Owners Manual (`DNHS5500EM_ENG_IM_003.pdf`), Parts List and
  Datasheet. Sibling legacy decks DN-S1200 and DN-S3700 each list a "MIDI Command List"; the
  HS5500 row does not.
- The 41-page owners manual contains no note or control-change table. It names a MIDI button,
  USB MIDI Control Mode, preset 28 "MIDI CH Set: 1&2 / 3&4 / 5&6" and preset 29 "Unit No Set:
  1 to 14 / ALL". No per-control wire identity appears.
- A PDF that circulates online as an HS5500 MIDI chart is the DN-HC4500 manual (108 pages; the
  string "HS5500" does not appear).
- The repo already records the gap: `docs/controller/research/DENON-PRIME-4-PLUS-sources.md`
  lists Denon models that do ship MIDI documents, and the HS5500 is not among them;
  `tools/deck-diagrams/catalog/controllers.json` marks this device `"docs": "mixxx"`,
  `"doc_tier": 2`, `"checksum": "n/a"`; `tools/deck-diagrams/devices/denon-dn-hs5500/footnotes.md`
  states "No official vendor MIDI PDF".

## Clean-room consequence

Under ADR-0137 (`(private path)`), Open DJ only
commits wires read from a vendor document or from a probe-rig capture. Mixxx's
`Denon DN HS5500.midi.xml` is GPL-2.0-or-later and may cross-check a chart wire only; with no
chart, it is not a primary source. `tools/deck-diagrams/devices/denon-dn-hs5500/midi.json` is
Mixxx-derived (`"mixxx_gpl": true` in the ingest) and the issue forbids treating it as a source.

No `denon-dn-hs5500` entry is registered in `DEVICE_MAP_REGISTRY` until
`tools/controller-probe/capture.py` or a new vendor document supplies verifiable wires.

## Surface notes for a future map

- **Mixer:** The unit is a single-deck player. It has no channel faders and no crossfader, so
  acceptance criteria that require both channel faders and a crossfader cannot be met. Precedent:
  `apps/webui/frontend/src/lib/rb/midi/maps/pioneer-cdj-3000.ts` (live single-deck map with no
  mixer-channel or crossfader bindings).
- **FX (manual p.3):** Flanger, Filter, Echo, Tail Echo, Echo Loop with Dry/Wet; platter Brake,
  Dump, Reverse; Mirror Mix. Open DJ's `MidiAction` union in
  `apps/webui/frontend/src/lib/rb/midi/midi-types.ts` and `PAGE_ACTIONS` in
  `apps/audio-engine/src/midi.rs` define no beat-FX or colour-FX action today; adding one is a
  wire-contract change in two languages and is outside the map-only scope of #10821.
- **Practice scaffold:** `tools/deck-diagrams/devices/denon-dn-hs5500/` has no `device.json` or
  `panel.json`, so `tools/deck-diagrams/scripts/new_panel.py` cannot scaffold a practice replica
  yet (follow-up work, PRIME 4+ pattern).
