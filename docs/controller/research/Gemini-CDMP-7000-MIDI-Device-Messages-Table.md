# Gemini CDMP-7000 MIDI device messages table

Vendor document, not committed. Used for issue #10828 (Deck 1 / L port map).

| Document | URL | sha256 |
| --- | --- | --- |
| Gemini CDMP-7000 user manual (18,429,390 bytes; PDF creation date 19 Nov 2012) | The original geminidj.com URL is no longer resolvable. Public mirrors include manualmachine.com/gemini/cdmp7000/21367903-user-manual and manuals.online/gemini/dj-equipment/cdmp-7000/user-manual/21rb3 (not byte-checked). | `cec06edcbc28f996c118b5a458dc67949b8d253d9b0cb224a9824d4ebe94b210` |

Wayback-style SHA-1 digest (base32): `PYQQEPTTDP6AMXM2VXMNGWSNPP2OEI2I`.

## Table location

English: page 19, **MIDI DEVICE MESSAGES TABLE** (columns: Controller Parameter,
Software Parameter, Send Number, Min Value, Max Value).

Spanish copy: **TABLA DE MENSAJES DE UNIDAD MIDI** (equivalent rows).

## What the chart omits

- **MIDI channel**: not listed. The unit exposes separate USB MIDI ports
  `CDMP-7000 L` and `CDMP-7000 R`; this map uses channel 1 on the L port (Deck 1).
- **Note vs CC**: not listed. Kind was cross-checked read-only against the Mixxx
  GPL preset `Gemini CDMP-7000 L audio.midi.xml` (not copied into this repo).

## Send numbers used by the L-port map (by name)

Cue (1), Play/Pause (2), pitch bend +/- (3, 4), HotCue 1-3 (5-7), MASTER TEMPO (13),
Select/Load Track (15), Loop In (16), Loop Out/Adjust (17), Reloop/Exit (18), Jogwheel
(32), Pitch Slider (33).

## Hints only (same table, not bound)

MEMO (8), track search (9-10), browse left/right (11-12), VINYL (14), BPM (19), effect
select 1-3 (20-22), jog touch (24), LOCK/RANGE/REVERSE/SLIP (28-31), track skip (34-35),
HOLD effect (36), DRY/WET CC (41).

## Documented but kind unverifiable (not wired)

Send numbers 23, 25, 26, 27, 37, 38, 39, 40, 42, 43, 44 (44 is the 14-bit pitch row).

## Hardware notes

- The CDMP-7000 mixer section is **analog**; the MIDI table has no channel fader,
  crossfader, EQ or trim rows.
- No SYNC button; MASTER TEMPO (13) is repurposed as beat-sync toggle in software.
- `tools/deck-diagrams/devices/gemini-cdmp-7000-l-audio/midi.json` is a plate stub,
  not a wire source.

Not run on hardware.
