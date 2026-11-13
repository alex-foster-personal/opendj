"""Build the XDJ-AZ practice-replica panel pair (ADR-0137, deck-replica checklist).

Writes two files under tools/deck-diagrams/devices/xdj-az/:

- panel.reference.json: the measurement of record. One pixel box per control, read off
  the top-panel photo embedded in the official "XDJ-AZ List of MIDI messages" PDF
  (page 1, image 0, 1217 x 710 px), converted to millimetres with separate x and y
  scales. The photo itself never enters the repo; only the numbers do.
- panel.json: the same trace normalised for the renderer (0.5 mm centres, whole-mm
  sizes, row and column mates snapped onto one line) with the wire identity each
  control sends, read from XDJ_AZ_MAP (apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts).

Sources:
- Panel size 895 x 504.1 mm: XDJ-AZ Instruction Manual, Specifications, "Max. external
  dimensions (W x D x H) 895 x 504.1 x 133.4 mm".
- Wire identities and fig numbers: the official MIDI message list (sha256 in
  docs/controller/research/XDJ-MIDI-Message-Lists.md), as transcribed into XDJ_AZ_MAP.
- Positions: measured trace of that PDF's top-panel photo (calibration below).

Layout facts the trace established:
- The right deck is a translation of the left deck by 730 px (591 mm), not a mirror.
  Every right-deck box is the left-deck box shifted; 22 right-deck controls were read
  independently to measure the repeat spread.
- The left deck carries deck 1 wires (channel 1) and the right deck deck 2 wires
  (channel 2), the unit's power-on selection. DECK SELECT boxes carry the channel of
  the deck they select.
- The four mixer strips sit in the order 3, 1, 2, 4 from left to right, as on the panel.

Run: uv run --locked python tools/deck-diagrams/scripts/build_xdj_az_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from build_xdj_az_panel_build import build  # noqa: E402
from build_xdj_az_panel_trace import (  # noqa: E402, F401
    DEVICE,
    OUT_DIR,
    PANEL_OUT,
    PANEL_W_MM,
    REFERENCE_OUT,
)
from build_xdj_az_panel_wires_deck import (  # noqa: E402, F401
    Control,
    PAD_MODE_OFFSETS,
    PAD_MODE_SELECTORS,
    Wire,
)

if __name__ == "__main__":
    panel_doc, reference_doc = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PANEL_OUT.write_text(json.dumps(panel_doc, indent=1) + "\n", encoding="utf-8")
    REFERENCE_OUT.write_text(json.dumps(reference_doc, indent=1) + "\n", encoding="utf-8")
    n = len(panel_doc["controls"])  # type: ignore[arg-type]
    print(
        f"[OK] wrote {PANEL_OUT} and {REFERENCE_OUT} ({n} controls, repeat spread {reference_doc['trace']['repeat_spread_mm']} mm)"
    )  # type: ignore[index]
