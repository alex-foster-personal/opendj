# Denon DJ PRIME 4+ sources (practice replica, epic #9366, ADR-0137)

Clean-room record for `tools/deck-diagrams/devices/denon-prime4-plus/` and
`apps/webui/frontend/src/lib/rb/midi/maps/denon-prime4-plus.ts`. Only numbers read off the
documents below are committed; no PDF, render, photo, logo or font enters the repo.

## Official documents used

| Document | URL | sha256 | Used for |
| --- | --- | --- | --- |
| "User Guide - PRIME 4, PRIME 4+, PRIME 2, PRIME GO, PRIME GO+, SC LIVE 4, SC LIVE 2", Manual Version v5.3.0 (110 pages, A4), linked from the PRIME 4+ row of denondj.com/downloads | `https://cdn.inmusicbrands.com/engine/53/PRIME%204%2C%20PRIME%204%2B%2C%20PRIME%202%2C%20PRIME%20GO%2C%20PRIME%20GO%2B%2C%20SC%20LIVE%204%2C%20SC%20LIVE%202%20-%20User%20Guide%20-%20v5.3.0.pdf` | `a16b93d9cb97df95546e032435ee24e7fcd9520bbf88ca076e5d98b023a45402` | page 6: "All features and functions of PRIME 4 mentioned throughout this guide also apply to PRIME 4+ unless otherwise noted", and the Top Panel figure; pages 6 to 10: the numbered control list (`fig` values); page 101: Technical Specifications, Dimensions 28.68" x 19.56" x 4.08", 728.47 x 496.82 x 103.63 mm (W x D x H) |
| denondj.com PRIME 4+ product page | `https://www.denondj.com/prime-4-plus` | (web page) | 4 decks, 4 channels, 16 pads, dimensions in inches (agree with the guide) |
| denondj.com downloads page | `https://www.denondj.com/downloads.html` | (web page) | checked Sun 8 Nov 2026 for a MIDI document: none exists for PRIME 4 or PRIME 4+ |

## Top-panel figure render (the traced image)

The Top Panel figure on page 6 is a vector drawing, not an embedded photo, so the traced image
is a deterministic render of that page:

```
pdftoppm 26.08.0 -png -r 400 -f 6 -l 6 <guide.pdf>    # 3307 x 4678 px
sha256 1f1a44b399fc3d69cad91cd527a6efe9e41cc34d5d0a8986a3f0ee2b5d1a6146
```

Control boxes were read as the figure's own path rectangles (PyMuPDF `page.get_drawings()`,
scaled by 400/72 into that pixel frame), after excluding the 143 numbered callout discs the
guide overlays on the figure (black fill, 77 px diameter). The plate outline spans x 360..2943
and y 1177..2680 px; the published 728.47 mm width over 2584 px gives 0.28192 mm per px,
applied to both axes (every round control is round in the figure). The plate as drawn is
1504 px = 424.0 mm of the published 496.82 mm depth; the remaining 72.8 mm is the sloped front
face (the figure draws its headphone jack and knob below the plate line) and the rear connector
edge, neither of which carries a top-panel control. The trace puts y 0 at the plate's rear edge,
so if part of that remainder lies behind the plate every control shifts equally in y, which
`control_position_error.py` cannot detect; the relative layout is unaffected.

An independent second pass flood-filled the dark body of eleven controls that no callout disc
touches, on the raster render rather than the vector data; the largest centre difference was
1.5 px (0.42 mm), recorded as `repeat_spread_mm` 0.5 in `panel.reference.json`.

## MIDI: no published message list

Denon DJ publishes no MIDI message list, implementation chart or specification for the
PRIME 4+ (or the PRIME 4). The downloads page lists MIDI documents only for the LC6000,
MCX8000, MC6000 Mk2 and the legacy DN-HC4500, DN-HD2500, DN-S1200 and DN-S3700. The unit
sends MIDI only in computer (controller) mode, and the vendor's own catalog row in
`tools/deck-diagrams/catalog/controllers.json` already records "no official MIDI chart".

Consequence (clean-room rule, ADR-0137): `DENON_PRIME4_PLUS_MAP` binds nothing and lights
nothing. Every control carries a placeholder wire on MIDI channel 16 (sequential in layout
order; notes for buttons and pads, CCs for everything else) that exists only so the practice
layout joins to the map's hint table and renders inert with the PARITY-TODO title. The map is
not in `DEVICE_MAP_REGISTRY` and must not be until a published list or a probe-rig capture
replaces every placeholder.

## Out of scope on the top panel

Touchscreen, platter displays, FX OLED displays, channel and master level meters (displays),
the SD card slot and USB 1/2 ports (media slots), and everything on the front and rear panels.
