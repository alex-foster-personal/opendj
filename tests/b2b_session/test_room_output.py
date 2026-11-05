"""B2BROOM-01..03, pure half: the speakers store, the ``room`` wire shape and the room verdict.

[if] both laptops play to the room [then] the verdict is both with double_audio, [else stop].
[if] neither does [then] none with silent_room, [else stop].
[if] either side is not known [then] unknown, never a guessed side, [else stop].
[if] the store holds no choice [then] speakers are on and the source says unset, [else stop].
[if] the mirror's room_output is missing or malformed [then] the wire field is None, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.b2b_session import room_output
from apps.b2b_session.on_air import OnAirContext
from apps.b2b_session.peer_now_playing import shape_now_playing

pytestmark = [pytest.mark.requirement("B2BROOM-01"), pytest.mark.requirement("B2BROOM-02"), pytest.mark.requirement("B2BROOM-03")]

VERDICTS = Path(__file__).resolve().parents[1] / "fixtures" / "b2b-room-verdicts.json"


def _cases() -> list[dict[str, Any]]:
    cases = json.loads(VERDICTS.read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 12, "the shared table lost rows"
    return cases


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['local']}-{c['peer']}")
def test_room_verdict_matches_the_shared_table(case: dict[str, Any]) -> None:
    verdict = room_output.room_verdict(case["local"], case["peer"])
    assert verdict["room"] == case["room"]
    assert (verdict["warning"] or {}).get("code") == case["warning"]


def test_every_verdict_is_reached_by_the_table() -> None:
    assert {c["room"] for c in _cases()} == {"this_laptop", "their_laptop", "both", "none", "unknown"}


def test_store_round_trips_and_unset_is_on(tmp_path: Path) -> None:
    path = tmp_path / room_output.SPEAKERS_FILENAME
    assert room_output.read_speakers(path) == (True, "unset")
    room_output.write_speakers(path, False)
    assert room_output.read_speakers(path) == (False, "stored")
    room_output.write_speakers(path, True)
    assert room_output.read_speakers(path) == (True, "stored")


@pytest.mark.requirement("B2BROOM-05")
def test_a_stored_choice_can_be_cleared_back_to_unset(tmp_path: Path) -> None:
    """[if] a stored Speakers choice cannot be cleared, or clearing an unset one fails [then] `source` is `stored` for ever."""
    path = tmp_path / room_output.SPEAKERS_FILENAME
    assert room_output.clear_speakers(path) is False, "clearing a choice that was never stored says so, and does not fail"
    room_output.write_speakers(path, False)
    assert room_output.read_speakers(path) == (False, "stored")
    assert room_output.clear_speakers(path) is True
    assert room_output.read_speakers(path) == (True, "unset"), "if the cleared choice is still read as stored then broken"
    assert not path.exists()


def test_store_refuses_a_corrupt_record_and_a_non_bool(tmp_path: Path) -> None:
    path = tmp_path / room_output.SPEAKERS_FILENAME
    path.write_text(json.dumps({"speakers_on": "no"}), encoding="utf-8")
    with pytest.raises(TypeError, match="not a speakers record"):
        room_output.read_speakers(path)
    with pytest.raises(TypeError):
        room_output.write_speakers(path, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    [None, {}, {"speakers_on": 1, "room_path": "room_only"}, {"speakers_on": True}, {"speakers_on": True, "room_path": "both"}],
)
def test_malformed_room_output_is_none_never_a_default(raw: Any) -> None:
    assert room_output.shape_room_output(raw) is None


def test_now_playing_carries_the_room_field() -> None:
    mirror = {"decks": {}, "room_output": {"speakers_on": False, "room_path": "room_only"}}
    answer = shape_now_playing(mirror, 0.1, "Ana", 3.0, OnAirContext(link_joined=True, speakers_on=None))
    assert answer["room"] == {"speakers_on": False, "room_path": "room_only"}
    assert shape_now_playing({"decks": {}}, 0.1, "Ana", 3.0, OnAirContext(link_joined=True, speakers_on=None))["room"] is None
