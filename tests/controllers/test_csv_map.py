"""Vendor MIDI CSV to controller map, and a diff against the hand-written DDJ map (CTRL-12243).

[if] a vendor MIDI CSV is supplied [then] it yields a map and a non-mutating diff, [else stop].
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from apps.controllers.csv_map import CsvMapError, generate_map
from apps.controllers.ddj_map import diff_maps, hand_written_bindings
from apps.webui.server.routes.midi_maps import MidiMapDoc

pytestmark = pytest.mark.requirement("CTRL-12243")

# Synthesized vendor export (not a real vendor file): 7 supported rows, 1 unsupported.
FIXTURE_CSV: Path = Path(__file__).resolve().parent / "fixtures" / "vendor_midi_export.csv"
HEADER: str = "control,deck,shift,message,channel,number\n"

EXPECTED_SOURCES_AND_ACTIONS: list[tuple[dict[str, Any], dict[str, Any]]] = [
    ({"ch": 1, "kind": "note", "id": 11}, {"type": "deck_play_toggle", "deck": 1}),
    ({"ch": 1, "kind": "note", "id": 12}, {"type": "deck_cue", "deck": 1}),
    ({"ch": 8, "kind": "note", "id": 0}, {"type": "deck_hot_cue", "slot": "A", "deck": 1}),
    ({"ch": 1, "kind": "cc", "id": 19}, {"type": "mixer_channel", "target": "fader", "deck": 1}),
    ({"ch": 1, "kind": "cc", "id": 7}, {"type": "mixer_channel", "target": "eq", "band": "high", "deck": 1}),
    ({"ch": 7, "kind": "cc", "id": 31}, {"type": "mixer_global", "target": "crossfader"}),
    ({"ch": 2, "kind": "note", "id": 11}, {"type": "deck_play_toggle", "deck": 2}),
]


def _validated(document: dict[str, Any]) -> MidiMapDoc:
    """Validate the document the way the web UI does before it reaches the domain diff."""
    return MidiMapDoc.model_validate(document)


def _hand_document(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    """Synthesized hand-written DDJ device map in the installed MidiMapDoc shape."""
    return {
        "id": "ddj-test",
        "vendor": "Pioneer DJ",
        "model": "DDJ-TEST",
        "nameMatch": "DDJ-TEST",
        "bindings": [
            {**binding, "provenance": {"tier": "hand", "cite": "hand-written test map"}} for binding in bindings
        ],
    }


def _plain(bindings: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [{"source": b["source"], "action": b["action"], "shift": b["shift"]} for b in bindings]


def test_valid_csv_keeps_every_supported_row() -> None:
    """[if] a valid vendor CSV is supplied [then] every supported row and value is in the map, [else stop]."""
    generated = generate_map(FIXTURE_CSV)
    assert [(b["source"], b["action"]) for b in generated.bindings] == EXPECTED_SOURCES_AND_ACTIONS
    assert [(row.row, row.control) for row in generated.unsupported] == [(9, "BEAT FX ON")]
    assert generate_map(FIXTURE_CSV) == generated


def test_diff_names_added_removed_and_changed() -> None:
    """[if] generated and hand-written maps differ [then] the diff names every changed binding, [else stop]."""
    generated = _plain(generate_map(FIXTURE_CSV).bindings)
    hand = copy.deepcopy(generated)
    removed_binding = hand.pop()  # deck 2 play: in CSV only -> added
    hand[0]["action"] = {"type": "deck_cue", "deck": 1}  # changed
    hand_only = {
        "source": {"ch": 1, "kind": "note", "id": 99},
        "action": {"type": "deck_cue", "deck": 2},
        "shift": False,
    }
    hand.append(hand_only)  # removed
    document = _hand_document(hand)
    before = copy.deepcopy(document)
    diff = diff_maps(generated, hand_written_bindings(_validated(document)))
    assert diff.added == (removed_binding,)
    assert diff.removed == (hand_only,)
    assert [(c.source, c.hand_action, c.generated_action) for c in diff.changed] == [
        ((1, "note", 11, False), {"type": "deck_cue", "deck": 1}, {"type": "deck_play_toggle", "deck": 1})
    ]
    assert document == before


@pytest.mark.parametrize(
    "rows",
    [
        "PLAY/PAUSE,1,0,Note,1,11\nCUE,1,0,Note,1,11\n",
        "PLAY/PAUSE,1,0,Note,1,11\nPLAY/PAUSE,1,0,Note,1,12\n",
        "PLAY/PAUSE,1,0,Note,17,11\n",
        "PLAY/PAUSE,1,0,Note,1,200\n",
        "PLAY/PAUSE,1,0,Sysex,1,11\n",
        "PLAY/PAUSE,,0,Note,1,11\n",
        "PLAY/PAUSE,1,0,Note,1\n",
        "CROSSFADER,1,0,CC,7,31\n",
        "HOT CUE Z,1,0,Note,8,0\n",
        "PLAY/PAUSE,1,maybe,Note,1,11\n",
    ],
)
def test_malformed_or_duplicate_rows_raise(tmp_path: Path, rows: str) -> None:
    """[if] CSV rows are malformed or duplicated [then] the generator raises, [else stop]."""
    path = tmp_path / "export.csv"
    path.write_text(HEADER + rows, encoding="utf-8")
    with pytest.raises(CsvMapError):
        generate_map(path)


def test_matching_maps_have_an_empty_diff() -> None:
    """[if] the generated map matches the hand-written one [then] the diff is empty, [else stop]."""
    generated = _plain(generate_map(FIXTURE_CSV).bindings)
    diff = diff_maps(generated, hand_written_bindings(_validated(_hand_document(copy.deepcopy(generated)))))
    assert diff.is_empty
    assert (diff.added, diff.removed, diff.changed) == ((), (), ())
