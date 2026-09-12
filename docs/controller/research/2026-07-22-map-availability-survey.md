# MIDI Map Availability Survey - 35 Popular Devices (Wed 22 Jul 2026)

Research by a Sonnet subagent. Question: does the theory-first flow (fetch official MIDI
message list -> auto-map -> spot-check) generalize beyond the DDJ-FLX10?

## Summary stats

- 35 hardware devices surveyed (+ Algoriddim djay's hardware program as a 36th row).
- 17 of 35 (~49%) have a full standalone official MIDI message list from the vendor.
- AlphaTheta/Pioneer: 14 of 14 (100%). Every controller, player, and mixer surveyed has a
  consistently-named "MIDI Message List" PDF - a structural house convention, not a
  one-off. (Mixers/players are often MIDI-OUTPUT-ONLY: DJM-900NXS2, DJM-A9, CDJ lists
  cover surface out; deep integration is Pro DJ Link, closed.)
- Reloop: 2 of 4, split by generation - Beatpad 2 and MIXTOUR have official PDFs
  (Mixtour direct link: reloop.com/media/custom/upload/Reloop-Mixtour_MIDI-Map.pdf);
  newest flagships (Mixon 8, Mixtour Pro) do not.
- Denon DJ: 1 of 4 - only the LC6000 expansion unit has a published "MIDI Specification
  v1.0"; Prime 4 and SC6000 have nothing official.
- NEVER publish a static list in the current generation: Native Instruments (0/5 - live
  Controller Editor app instead of a document, HID-first hardware), Numark (0/2),
  Hercules current Inpulse line (0/1 - though their LEGACY gear shipped MIDI PDFs),
  Roland DJ line (0/2 - breaking Roland's own synth-division convention), Rane
  post-inMusic (0/3 - pre-acquisition Rane Corp shipped full MIDI appendices).
- Algoriddim djay: native maps for 50+ controllers are compiled into the app - a
  supported-hardware LIST is published, the underlying MIDI/HID tables are not.
  Unsupported gear = MIDI Learn + user-shared map files only. Several deep integrations
  (FLX10) mix MIDI with undocumented HID ("hidden HID commands" per community threads).
- FLX10 caveat even on the fast path: jog screens, VU meters, stem-FX indicator lights
  run over undocumented HID, outside the (perfect) MIDI PDF.

## Notable generational REGRESSION pattern

Older/simpler SKUs often had official docs (Hercules Compact/Instinct, legacy Rane
SIXTY-ONE/-TWO, Reloop Beatpad 2/Mixtour); the newest flagships dropped them (Inpulse,
Rane One/Seventy-Two/Twelve, Mixon 8, Mixtour Pro). Documentation availability is
getting WORSE, not better, outside AlphaTheta.

## Verdict (agent's, verbatim in substance)

The FLX10 result does not generalize. Full official wire-level MIDI documentation is
essentially an AlphaTheta/Pioneer house convention, not an industry norm. Theory-first
auto-map succeeds ~100% on Pioneer gear but has no input at all for roughly half the
market (Denon, NI, Numark, Hercules, Roland DJ, post-acquisition Rane). The consistently
available fallback for that half is the community-map layer (Mixxx repo/wiki, VirtualDJ
definitions, DJ TechTools, forum files) - present for nearly every popular device but
reverse-engineered, varying in completeness. Build the pipeline TWO-TIER from day one:
official-PDF auto-map as the fast path, community-DB as a mandatory fallback tier with a
human spot-check gate - the fallback is a day-one need for half of any real catalog, not
an edge case.

## Full device table

See the subagent transcript for the complete 36-row table with links; key rows:
- DDJ-FLX4/FLX6/FLX10/1000/800/REV1/REV5/REV7, CDJ-2000NXS2/3000, DJM-900NXS2/A9,
  XDJ-RX3/XZ: all official PDFs (players/mixers MIDI-out-only).
- Denon Prime 4 / SC6000 / X1850: no official doc; Mixxx/VirtualDJ community maps.
- LC6000: official MIDI Specification v1.0.
- Traktor Kontrol S2/S3/S4 MK3, X1 MK2, Z2: no static PDF; Controller Editor app; Mixxx.
- Numark Mixtrack Pro FX / Platinum FX: community-only (Mixxx + third-party manual).
- Hercules Inpulse 500: closed .djm files for DJUCED; community reverse-engineered maps.
- Roland DJ-505/808: no MIDI implementation chart; Mixxx community.
- Rane One / Seventy-Two / Twelve MK2: no confirmed charts; community WIP journals.
- Reloop Mixtour: OFFICIAL PDF (our second test device - fast path applies).

## FLX4 follow-up (Sat 12 Sep 2026, issue #1855)

| Field | Value |
| --- | --- |
| Device | DDJ-FLX4 |
| Official PDF | https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX4/DDJ-FLX4_MIDI_message_List_E1.pdf |
| sha256 | `e03ac376ebaa2d461c1a2c832c3c182e3579c893a1531944ebac1b5ce620388f` |
| Expected map | `tools/controller-probe/flx4_expected_map.json` (control_count = 402) |
| Runtime map | `apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts` (bindings.length = 65) |
| Status | Theory-first complete; hardware adjudication pending Jake's boat, week commencing Mon 14 Sep 2026 |
| Out of band | HID jog screen / meters; beat-sync (#1777) |
