# RANE FOUR MIDI message list and panel sources (research note)

Recorded Sun 8 Nov 2026 for the RANE FOUR practice replica (epic #9366, ADR-0137). Theory-first: no
unit was connected. Only numbers derived from the vendor documents live in the repository; no vendor
PDF, image, logo or crop is committed (`(private path)`).

## The finding: no published MIDI message list

RANE (inMusic) publishes no MIDI message list, implementation chart or MIDI map for the FOUR. Checked
Sun 8 Nov 2026:

| Where | What was there |
| --- | --- |
| https://www.rane.com/four/ (product page, Specifications) | audio specs, pad and display counts, dimensions, weight; no document links |
| https://www.rane.com/downloads/ | drivers and firmware only (the page lists no MIDI document for the FOUR) |
| https://support.rane.com/en/support/solutions/articles/69000828752-rane-four-frequently-asked-questions | feature overview, setup, box contents, dimensions and weight; no MIDI section, no PDF links |
| "FOUR User Guide" v1.0 and v1.2 (below) | no MIDI chart or note/CC table; the only MIDI mentions are that the jog displays are driven by Serato DJ "via MIDI messages" and that a deck sends MIDI only while its source selector is set to USB A or B |
| https://cdn.inmusicbrands.com/rane/four/ (the host of the user guide and firmware notes) | `FOUR - User Guide - v1.0.pdf`, `FOUR - User Guide - v1.2.pdf`, the firmware release notes; probed names for a MIDI specification, MIDI map or MIDI implementation document all answer 404 |

`(private path)` already records that post-inMusic RANE products
publish no wire-level MIDI docs. The consequence for the replica process is in the last section.

## Official sources used

| Field | Value |
| --- | --- |
| User guide URL | https://cdn.inmusicbrands.com/rane/four/FOUR%20-%20User%20Guide%20-%20v1.2.pdf |
| Filename | `FOUR - User Guide - v1.2.pdf` (39 pages, 14,597,305 bytes, "Manual Version 1.2", modified Fri 20 Sep 2024) |
| sha256 | `bf80bf957e3897aa46e57a46db094623dc07a9a2b94e47ec02e1c6fd6ab433bf` |
| Earlier edition | `FOUR - User Guide - v1.0.pdf` (38 pages, 7,473,403 bytes), sha256 `258afa333c51f52898ae3c72e8df75769078503d9549becc1f1baf16d2addb22`; same control list, read for cross-checking |
| Fetch date | Sun 8 Nov 2026 |
| Panel size | User guide v1.2, Technical Specifications (page 36): Dimensions (width x depth x height) 30.84" x 15.84" x 4.08"; product page Specifications: 783.34 x 402.34 x 103.63 mm, weight 8.27 kg including platters |
| Jog wheel | user guide, Deck Controls item 7: 8" capacitive touch jog wheel (the product page says 8.5"; the measured outer bezel below is 221.7 mm) |
| Control names and numbering | user guide, Features: Top Panel (Deck Controls 1..17, Performance Pads 1..13, FX Section 1..9, Mixer Controls 1..22); these numbers are the `fig` values in the builder |

## Top-panel image used for the position trace

The user guide carries no single top-panel figure (its Features pages show three separate crops at
different scales), so the trace was made on the product page's top-down product render:

| Field | Value |
| --- | --- |
| Image URL | https://cdn.builder.io/api/v1/image/assets%2F47c5d023e7b3451a86ac03602c65dce8%2F18ce606d55974629aeaf6c8e8e322b62 (the hero image of https://www.rane.com/four/) |
| Format and size | WebP with alpha, 1680 x 884 px |
| sha256 (as served) | `deed73a93db2593cb452ee0f7fbf5f7ec7d8ea50cddf066bb62395021a614b11` |
| sha256 (lossless PNG conversion traced on) | `13352031fba662fcf07ba0fb7b1c0f361580784117620a7996963a9f3dd27d7e` |
| Plate body | opaque columns 68..1611 (1544 px) over rows 74..816 (743 px); the alpha channel delimits the plate exactly |
| Calibration | 783.34 mm / 1544 px = 0.50735 mm/px on x; y uses the same scale because the render is isotropic (SLIP, CENSOR and SHIFT buttons 23 to 25 px square, PLAY/PAUSE 69 x 69 px); the 743 visible rows cover 377.0 mm of the published 402.34 mm depth, the rest being the front-panel controls and rear connectors that a top view does not show |

The traced pixel boxes live in `tools/deck-diagrams/scripts/build_rane_four_panel.py`; the derived
measurement is `tools/deck-diagrams/devices/rane-four/panel.reference.json`. Lit controls (pads,
buttons) were read as the bounding box of their lit body (luminance above 150 or saturation above
90 within the plate); knob rings and caps as the bounding box of the ring pixels inside a search
disk; dark controls (jogs, faders, crossfader, encoders, paddles, switches, dark knobs) as edges
read off the luminance profile of the row and column through the control. The image never enters
the repository.

## What the replica process can and cannot do without a MIDI list

- `panel.reference.json` (the measurement of record) needs no wire identities, so it ships here
  with `"measured": true`, and `scripts/skins/control_position_error.py` scores the normalised
  layout PASS against it.
- `panel.json` cannot ship: `opendj-panel-layout/1` requires a `midi` wire per control and a
  `device_map`, `resolveControl` throws on any wire the map neither binds nor hints, and every
  wire for this unit would be invented. `new_panel.py` cannot scaffold it either (it reads a map
  from the registry export), so there is no `DEVICES` row, no map, no registry entry and no
  practice route for `rane-four` yet.
- The wires need a hardware capture on a unit (`tools/controller-probe/capture.py`, the TIER 3
  path in `DATA-SOURCES-INDEX.md`) or a RANE publication. Once they exist, the builder's control
  rows take their `midi` and `shift_midi` and the rest of the checklist (steps 4, 6, 7) follows.
  Mixxx ships no mapping for this unit, and community maps would not satisfy the clean-room rule
  in any case.
