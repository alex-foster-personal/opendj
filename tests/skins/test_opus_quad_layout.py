"""requirement: CTRL-14846 (AlphaTheta OPUS-QUAD deck diagram: per-deck MIDI rows, hotspots on the controls)

[if] a deck row exists only for deck 1 (ch 1) [then] decks 2 to 4 show deck 1's wires: broken, [else stop].
[if] a hotspot's wire is not the row it names (channel, type, code) [then] broken, [else stop].
[if] a deck fig lacks a left (deck 1/3, ch 1) or right (deck 2/4, ch 2) hotspot [then] broken, [else stop].
[if] a hotspot covers its plate callout label instead of the control [then] broken, [else stop].
[if] the layout fails its schema, repeats an id, leaves the plate or fails checksum.py [then] broken, [else stop].
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest

pytestmark = pytest.mark.requirement("CTRL-14846")

ROOT = Path(__file__).resolve().parents[2]
DECK_DIAGRAMS = ROOT / "tools" / "deck-diagrams"
DEVICE_DIR = DECK_DIAGRAMS / "devices" / "opus-quad"
SCHEMA = DECK_DIAGRAMS / "schema" / "control-layout.schema.json"

# Centre (percent of the plate) of each fig's callout label on source/plate-top.png,
# where the first version of this layout put every hotspot. 1-1 is left out: its
# label sits on the jog platter itself.
CALLOUT_LABELS: dict[str, tuple[float, float]] = {
    "1-2": (32.45, 82.2),
    "1-3": (33.9, 75.4),
    "1-4": (32.2, 61.5),
    "1-5": (32.2, 66.2),
    "1-6": (32.3, 54.8),
    "1-7": (32.7, 48.1),
    "1-8": (35.8, 48.2),
    "1-9": (27.6, 46.1),
    "1-10": (32.7, 36.5),
    "1-11": (25.0, 27.9),
    "1-12": (11.1, 28.1),
    "1-13": (6.4, 35.9),
    "1-14": (3.6, 40.9),
    "1-15": (3.9, 48.6),
    "1-16": (8.4, 48.7),
    "1-17": (6.6, 55.2),
    "1-18": (6.6, 69.5),
    "1-19": (2.6, 76.8),
    "1-20": (2.6, 86.5),
    "1-21": (9.7, 42.9),
    "1-22": (12.8, 42.9),
    "1-23": (15.9, 42.9),
    "1-24": (19.0, 43.0),
    "1-25": (22.1, 43.0),
    "1-26": (25.2, 43.0),
    "1-27": (28.3, 43.0),
    "1-28": (31.4, 43.0),
    "1-29": (12.5, 48.0),
    "1-30": (66.2, 32.8),
    "1-31": (47.5, 24.9),
    "2-1": (63.5, 62.2),
    "2-2": (63.8, 66.5),
    "2-3": (61.1, 73.5),
    "2-4": (63.8, 76.8),
    "2-5": (63.7, 81.1),
    "2-6": (63.9, 86.1),
    "2-7": (54.4, 64.3),
    "2-8": (39.5, 69.2),
    "2-9": (37.1, 57.6),
    "2-10": (41.6, 57.7),
    "2-11": (37.0, 61.9),
    "2-12": (41.7, 61.8),
    "2-13": (37.0, 65.7),
    "2-14": (41.8, 65.5),
    "2-15": (31.7, 25.5),
    "2-16": (31.6, 30.1),
    "3-1": (45.55, 90.7),
    "3-2": (46.15, 81.3),
    "3-3": (52.1, 27.5),
    "3-4": (51.6, 20.2),
    "3-5": (46.1, 35.6),
    "3-6": (45.8, 47.5),
    "3-7": (45.8, 53.4),
    "3-8": (46.0, 59.0),
    "3-9": (37.2, 75.0),
    "3-10": (37.2, 81.7),
    "3-11": (46.1, 70.6),
    "3-12": (61.0, 43.2),
    "3-13": (3.4, 29.7),
    "3-14": (3.3, 23.4),
    "4-1": (69.8, 17.6),
    "4-2": (62.4, 13.7),
    "4-3": (70.2, 14.0),
}


def _layout() -> dict:
    return json.loads((DEVICE_DIR / "layout.json").read_text())


def _midi() -> dict:
    return json.loads((DEVICE_DIR / "midi.json").read_text())


def _checksum_module() -> ModuleType:
    scripts = DECK_DIAGRAMS / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("deck_diagram_checksum", scripts / "checksum.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
    return module


def test_every_deck_row_has_its_own_row_for_decks_2_to_4() -> None:
    """[if] a deck row is listed for ch 1/2/3/4 [then] midi.json carries one row per deck channel, [else stop]."""
    rows = _midi()["controls"]
    by_name = {r["name"]: r for r in rows}
    deck1 = [r for r in rows if r["section"] in ("deck", "others") and r["ch"] == 1]
    assert len(deck1) >= 50
    for r in deck1:
        for deck in (2, 3, 4):
            twin = by_name.get(f"{r['name']}_deck{deck}")
            assert twin is not None, f"{r['name']} has no deck {deck} row"
            assert (twin["ch"], twin["type"], twin["code"], twin["layer"]) == (deck, r["type"], r["code"], r["layer"])


def test_each_hotspot_wire_is_the_row_it_names() -> None:
    """[if] a hotspot names a midi.json row [then] its channel, type and code are that row's, [else stop]."""
    by_name = {r["name"]: r for r in _midi()["controls"]}
    for c in _layout()["controls"]:
        m = c["midi"]
        row = by_name[m["name"]]
        assert (m["ch"], m["type"], m["code"]) == (row["ch"], row["type"], row["code"]), c["id"]


def test_each_deck_fig_has_a_left_and_a_right_hotspot() -> None:
    """[if] a deck fig is on the plate [then] deck 1/3 has a ch 1 hotspot left of the mixer and deck 2/4 a ch 2 one right of it, [else stop]."""
    deck = [c for c in _layout()["controls"] if c["section"] == "deck"]
    figs = {c["fig"] for c in deck}
    assert len(figs) == 31
    for fig in figs:
        chans = {c["midi"]["ch"] for c in deck if c["fig"] == fig}
        assert chans == {1, 2}, fig
    for c in deck:
        if c["fig"] in ("1-30", "1-31"):
            continue  # LOAD and the needle strip sit by the screen, one per side
        centre = c["x"] + c["w"] / 2
        assert (centre < 37) if c["midi"]["ch"] == 1 else (centre > 63), c["id"]


def test_hotspots_sit_on_controls_not_on_callout_labels() -> None:
    """[if] a hotspot is drawn [then] it does not cover its fig's callout label, [else stop]."""
    for c in _layout()["controls"]:
        label = CALLOUT_LABELS.get(c["fig"])
        if label is None:
            continue
        lx, ly = label
        inside = c["x"] <= lx <= c["x"] + c["w"] and c["y"] <= ly <= c["y"] + c["h"]
        assert not inside, f"{c['id']} covers the {c['fig']} label"


def test_layout_validates_fits_the_plate_and_passes_checksum(capsys) -> None:
    """[if] layout.json is shipped [then] it fits its schema, has unique ids, stays on the plate and checksum.py passes, [else stop]."""
    layout = _layout()
    jsonschema.validate(layout, json.loads(SCHEMA.read_text()))
    ids = [c["id"] for c in layout["controls"]]
    assert len(ids) == len(set(ids))
    for c in layout["controls"]:
        assert 0 <= c["x"] and c["x"] + c["w"] <= 100, c["id"]
        assert 0 <= c["y"] and c["y"] + c["h"] <= 100, c["id"]
    assert _checksum_module().main(DEVICE_DIR) == 0, capsys.readouterr().out
