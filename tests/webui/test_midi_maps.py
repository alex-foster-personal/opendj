"""Installed controller device maps: install, list, read, remove.

The document these routes carry is the SAME shape the browser registers at
runtime (camelCase, `DeviceMap` in apps/webui/frontend/src/lib/rb/midi/
midi-types.ts) plus per-binding provenance. Design:
(private path) section 3.1-3.2.

Regression lines:
  if a document with an action outside the union installs then broken
  if a binding without provenance installs then broken
  if a map id with a path separator escapes the maps dir then broken
  if PUT does not write <data>/state/controller-maps/<id>.json then broken
  if DELETE on an absent id returns 200 then broken
  if a LedRule with an unknown trigger kind installs then broken
  if an init message with a 0xF0 status installs then broken
  if a LedTrigger kind exists in midi-types.ts but not in the daemon then broken
  if a MidiAction kind in midi-types.ts has no server model then broken
  if ccButton installs on a note source or a non-button action then broken
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes.midi_maps import (
    _BUTTON_ACTIONS,
    _CONTINUOUS_ACTIONS,
    _LED_TRIGGER_MODELS,
    _RELATIVE_ACTIONS,
    MidiActionModel,
)


@pytest.fixture
def midi_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def _cite(tier: str = "learned") -> dict[str, Any]:
    return {
        "tier": tier,
        "cite": "learn wizard 2026-08-29T10:00:00Z, port TestDevice",
        "verified": True,
    }


def _doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schemaVersion": 1,
        "id": "test-device",
        "vendor": "TestCo",
        "model": "TestDevice 1",
        "nameMatch": "TestDevice",
        "bindings": [
            {
                "source": {"ch": 1, "kind": "note", "id": 11},
                "action": {"type": "deck_play_toggle", "deck": 1},
                "provenance": _cite(),
            },
            {
                "source": {"ch": 1, "kind": "cc", "id": 0},
                "action": {"type": "deck_pitch", "deck": 1, "lsbOffset": 32},
                "provenance": _cite("vendor-pdf"),
            },
        ],
    }
    doc.update(over)
    return doc


# ----- install + read back --------------------------------------------------


def test_put_installs_and_get_reads_the_same_document(
    midi_client: TestClient, tmp_path: Path
) -> None:
    r = midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    assert r.status_code == 200, r.text
    on_disk = tmp_path / "data" / "state" / "controller-maps" / "test-device.json"
    assert on_disk.exists(), "the document is the source of truth, so it must reach disk"
    assert json.loads(on_disk.read_text())["nameMatch"] == "TestDevice"

    got = midi_client.get("/api/v1/midi/maps/test-device")
    assert got.status_code == 200
    round_tripped = got.json()["bindings"][1]["action"]["lsbOffset"]
    assert round_tripped == 32, "camelCase survives the round trip"


def test_list_reports_what_the_ui_needs_without_shipping_every_binding(
    midi_client: TestClient,
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    r = midi_client.get("/api/v1/midi/maps")
    assert r.status_code == 200
    rows = r.json()["maps"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "test-device"
    assert row["nameMatch"] == "TestDevice"
    assert row["bindingCount"] == 2
    assert row["provenance"] == {"vendor-pdf": 1, "learned": 1}
    assert "bindings" not in row


def test_list_is_empty_rather_than_404_before_anything_is_installed(
    midi_client: TestClient,
) -> None:
    r = midi_client.get("/api/v1/midi/maps")
    assert r.status_code == 200
    assert r.json()["maps"] == []


def test_list_skips_a_map_removed_between_glob_and_read(
    midi_client: TestClient, tmp_path: Path
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    maps_dir = tmp_path / "data" / "state" / "controller-maps"
    (maps_dir / "gone-device.json").symlink_to(maps_dir / "never-written.json")
    r = midi_client.get("/api/v1/midi/maps")
    assert r.status_code == 200
    assert [row["id"] for row in r.json()["maps"]] == ["test-device"]


def test_put_replaces_an_existing_map_in_place(midi_client: TestClient) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    doc = _doc()
    doc["bindings"] = doc["bindings"][:1]
    assert midi_client.put("/api/v1/midi/maps/test-device", json=doc).status_code == 200
    assert midi_client.get("/api/v1/midi/maps").json()["maps"][0]["bindingCount"] == 1


# ----- fail fast at the edge ------------------------------------------------


def test_unknown_action_type_is_refused_with_the_binding_index(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["action"] = {"type": "deck_scratch", "deck": 1}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"
    assert "bindings[1]" in r.json()["detail"]["message"]


def test_binding_without_provenance_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    del doc["bindings"][0]["provenance"]
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"


def test_duplicate_source_in_one_map_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = dict(doc["bindings"][0]["source"])
    doc["bindings"][1]["action"] = {"type": "deck_cue", "deck": 1}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]["message"].lower()


def test_shift_modifier_bound_shift_true_is_refused(midi_client: TestClient) -> None:
    # shiftHeld starts false at runtime, so a shift_modifier bound shift:true
    # could never receive the press that would set shiftHeld true - see PR #511
    # review thread "Reject shifted shift modifiers".
    doc = _doc()
    doc["bindings"][0]["action"] = {"type": "shift_modifier"}
    doc["bindings"][0]["shift"] = True
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "shift_modifier" in r.json()["detail"]["message"]


def test_shift_modifier_not_itself_shifted_still_installs(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["action"] = {"type": "shift_modifier"}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_ordinary_binding_on_a_claimed_lsb_slot_is_refused(midi_client: TestClient) -> None:
    # bindings[1] is deck_pitch on ch1/cc0 with lsbOffset 32, claiming ch1/cc32
    # as its 14-bit LSB. webmidi.svelte.ts's lsbIndex is shift-independent and
    # checked before the shift-aware index, so an ordinary binding on ch1/cc32
    # - shifted or not - could never fire. See PR #511 review thread "Reject
    # controls that collide with a 14-bit LSB".
    doc = _doc()
    doc["bindings"].append(
        {
            "source": {"ch": 1, "kind": "cc", "id": 32},
            "action": {"type": "mixer_channel", "deck": 1, "target": "fader"},
            "provenance": _cite(),
        }
    )
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "14-bit LSB" in r.json()["detail"]["message"]


def test_shifted_binding_on_a_claimed_lsb_slot_is_still_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"].append(
        {
            "source": {"ch": 1, "kind": "cc", "id": 32},
            "action": {"type": "mixer_channel", "deck": 1, "target": "fader"},
            "shift": True,
            "provenance": _cite(),
        }
    )
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "14-bit LSB" in r.json()["detail"]["message"]


def test_ordinary_binding_on_a_different_channels_lsb_number_still_installs(
    midi_client: TestClient,
) -> None:
    # Same CC number (32) as the claimed LSB slot, but a DIFFERENT channel: the
    # LSB claim is keyed by (ch, cc), so this must not collide.
    doc = _doc()
    doc["bindings"].append(
        {
            "source": {"ch": 2, "kind": "cc", "id": 32},
            "action": {"type": "mixer_channel", "deck": 1, "target": "fader"},
            "provenance": _cite(),
        }
    )
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_two_deck_pitch_bindings_cannot_claim_the_same_lsb_slot(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"].append(
        {
            "source": {"ch": 1, "kind": "cc", "id": 1},
            "action": {"type": "deck_pitch", "deck": 2, "lsbOffset": 31},
            "provenance": _cite("vendor-pdf"),
        }
    )
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "14-bit LSB" in r.json()["detail"]["message"]


def test_eq_without_a_band_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["action"] = {"type": "mixer_channel", "deck": 1, "target": "eq"}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422


def test_band_on_a_non_eq_target_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["action"] = {
        "type": "mixer_channel",
        "deck": 1,
        "target": "fader",
        "band": "low",
    }
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422


def test_channel_outside_1_to_16_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["source"]["ch"] = 17
    assert midi_client.put("/api/v1/midi/maps/test-device", json=doc).status_code == 422


def test_pitchbend_must_carry_id_zero(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["source"] = {"ch": 1, "kind": "pitchbend", "id": 7}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "pitchbend" in r.json()["detail"]["message"].lower()


def test_invalid_name_match_regex_is_refused(midi_client: TestClient) -> None:
    doc = _doc(nameMatch="DDJ-[400")
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "nameMatch" in r.json()["detail"]["message"]


@pytest.mark.parametrize("beats", [0.0625, 0.125, 0.25, 0.5, 4])
def test_deck_beat_loop_fractional_beats_validate(beats: float) -> None:
    from apps.webui.server.routes.midi_maps import DeckBeatLoop

    action = DeckBeatLoop.model_validate({"type": "deck_beat_loop", "deck": 1, "beats": beats})
    assert action.beats == beats


@pytest.mark.parametrize("deck", [1, 2, 3, 4])
@pytest.mark.parametrize("action_type", ["deck_stem_eq_toggle", "deck_auto_loop_toggle"])
def test_controller_toggle_validates_on_all_decks(deck: int, action_type: str) -> None:
    from pydantic import TypeAdapter

    from apps.webui.server.routes.midi_maps import MidiActionModel

    action = TypeAdapter(MidiActionModel).validate_python(
        {"type": action_type, "deck": deck}
    )
    assert action.type == action_type
    assert action.deck == deck


@pytest.mark.parametrize("beats", [0, -0.5, float("inf"), float("nan")])
def test_deck_beat_loop_invalid_beats_are_refused(beats: float) -> None:
    from pydantic import ValidationError

    from apps.webui.server.routes.midi_maps import DeckBeatLoop

    with pytest.raises(ValidationError):
        DeckBeatLoop.model_validate({"type": "deck_beat_loop", "deck": 1, "beats": beats})


def test_deck_beat_loop_integer_beats_still_installs(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["action"] = {"type": "deck_beat_loop", "deck": 1, "beats": 4}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("beats", [-4, -0.5, 1, 4])
def test_deck_beat_jump_signed_beats_validate(beats: float) -> None:
    from apps.webui.server.routes.midi_maps import DeckBeatJump

    action = DeckBeatJump.model_validate({"type": "deck_beat_jump", "deck": 1, "beats": beats})
    assert action.beats == beats


@pytest.mark.parametrize("beats", [0, float("inf"), float("nan")])
def test_deck_beat_jump_invalid_beats_are_refused(beats: float) -> None:
    from pydantic import ValidationError

    from apps.webui.server.routes.midi_maps import DeckBeatJump

    with pytest.raises(ValidationError):
        DeckBeatJump.model_validate({"type": "deck_beat_jump", "deck": 1, "beats": beats})


def test_deck_beat_jump_binding_installs(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["action"] = {"type": "deck_beat_jump", "deck": 1, "beats": -4}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_button_action_on_a_cc_source_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["source"] = {"ch": 1, "kind": "cc", "id": 11}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "note source" in r.json()["detail"]["message"]


def test_continuous_action_on_a_note_source_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = {"ch": 1, "kind": "note", "id": 5}
    doc["bindings"][1]["action"] = {"type": "mixer_channel", "deck": 1, "target": "fader"}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "cc or pitchbend" in r.json()["detail"]["message"]


def test_continuous_action_bound_relative_on_cc_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = {"ch": 1, "kind": "cc", "id": 5}
    doc["bindings"][1]["action"] = {"type": "mixer_channel", "deck": 1, "target": "fader"}
    doc["bindings"][1]["relative"] = True
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "cannot be bound relative" in r.json()["detail"]["message"]


def test_14bit_deck_pitch_on_a_note_source_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = {"ch": 1, "kind": "note", "id": 5}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "cc" in r.json()["detail"]["message"]


def test_deck_pitch_without_lsb_offset_bound_relative_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["action"] = {"type": "deck_pitch", "deck": 1, "lsbOffset": None}
    doc["bindings"][1]["relative"] = True
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "relative" in r.json()["detail"]["message"]


def test_deck_pitch_lsb_offset_zero_is_refused(midi_client: TestClient) -> None:
    # offset 0 makes the LSB key collide with the MSB's own source id, so
    # webmidi.svelte.ts's _dispatch() drops every message as "LSB arrived
    # before any MSB" - see PR #511 review thread.
    doc = _doc()
    doc["bindings"][1]["action"] = {"type": "deck_pitch", "deck": 1, "lsbOffset": 0}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "lsbOffset" in r.json()["detail"]["message"]


def test_deck_pitch_lsb_offset_indexing_past_cc_127_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = {"ch": 1, "kind": "cc", "id": 120}
    doc["bindings"][1]["action"] = {"type": "deck_pitch", "deck": 1, "lsbOffset": 32}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "lsbOffset" in r.json()["detail"]["message"]


def test_deck_pitch_lsb_offset_within_range_still_installs(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = {"ch": 1, "kind": "cc", "id": 95}
    doc["bindings"][1]["action"] = {"type": "deck_pitch", "deck": 1, "lsbOffset": 32}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_shifted_14bit_deck_pitch_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["shift"] = True
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"
    msg = r.json()["detail"]["message"]
    assert "bindings[1]" in msg
    assert "shift" in msg


def test_shifted_7bit_deck_pitch_still_installs(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["shift"] = True
    doc["bindings"][1]["action"] = {"type": "deck_pitch", "deck": 1, "lsbOffset": None}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_browse_encoder_requires_a_cc_source_bound_relative(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["action"] = {"type": "browse_encoder"}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "browse_encoder" in r.json()["detail"]["message"]


def test_browse_encoder_bound_relative_on_cc_installs(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][1]["source"] = {"ch": 1, "kind": "cc", "id": 5}
    doc["bindings"][1]["action"] = {"type": "browse_encoder"}
    doc["bindings"][1]["relative"] = True
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "bad_pattern",
    ["(?P<foo>Test)", "(?P<foo>a)(?P=foo)", "(?i)Test", "(?#comment)Test", r"\ATest\Z"],
)
def test_name_match_syntax_the_browser_regex_engine_cannot_run_is_refused(
    midi_client: TestClient, bad_pattern: str
) -> None:
    doc = _doc(nameMatch=bad_pattern)
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "browser cannot run" in r.json()["detail"]["message"]


def test_ordinary_name_match_regex_still_installs(midi_client: TestClient) -> None:
    doc = _doc(nameMatch="DDJ-(400|FLX4)")
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_a_second_installed_map_cannot_reuse_a_name_match_already_in_use(
    midi_client: TestClient,
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    other = _doc(id="other-device")
    other["nameMatch"] = "TestDevice"
    r = midi_client.put("/api/v1/midi/maps/other-device", json=other)
    assert r.status_code == 422
    assert "test-device" in r.json()["detail"]["message"]


def test_a_second_installed_map_cannot_reuse_a_name_match_differing_only_by_case(
    midi_client: TestClient, tmp_path: Path
) -> None:
    """[if] case-equivalent nameMatch [then] 422 names clashing map, [else stop]."""
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    other = _doc(id="other-device", nameMatch="testdevice")
    r = midi_client.put("/api/v1/midi/maps/other-device", json=other)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"
    assert "test-device" in r.json()["detail"]["message"]
    assert not (tmp_path / "data" / "state" / "controller-maps" / "other-device.json").is_file()


def test_replacing_a_map_in_place_does_not_clash_with_its_own_name_match(
    midi_client: TestClient,
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    r = midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    assert r.status_code == 200, r.text


def test_replacing_a_map_in_place_may_change_only_the_case_of_its_name_match(
    midi_client: TestClient,
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    doc = _doc(nameMatch="TESTDEVICE")
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text


def test_put_body_schema_is_published_as_midi_map_doc(midi_client: TestClient) -> None:
    """[if] OpenAPI PUT schema [then] exact MidiMapDoc $ref, [else stop]."""
    spec = midi_client.app.openapi()
    request_body = spec["paths"]["/api/v1/midi/maps/{map_id}"]["put"]["requestBody"]
    assert request_body["required"] is True
    schema = request_body["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/MidiMapDoc"}


def test_malformed_json_body_is_refused_with_the_house_shape(midi_client: TestClient) -> None:
    r = midi_client.put(
        "/api/v1/midi/maps/test-device",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"


def test_non_object_body_is_refused_with_the_house_shape(midi_client: TestClient) -> None:
    r = midi_client.put("/api/v1/midi/maps/test-device", json=[1, 2])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"


def test_an_unknown_field_on_the_document_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["extraField"] = "typo"
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422


def test_an_unknown_field_on_a_binding_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["typo"] = True
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422


def test_an_unknown_field_on_an_action_is_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["action"] = {"type": "deck_play_toggle", "deck": 1, "typo": True}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422


def test_body_id_must_match_the_path_id(midi_client: TestClient) -> None:
    r = midi_client.put("/api/v1/midi/maps/test-device", json=_doc(id="other-device"))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_ID_MISMATCH"


@pytest.mark.parametrize("bad_id", ["../escape", "with/slash", "UPPER", "trailing-", "sp ace"])
def test_a_map_id_can_never_escape_the_maps_directory(midi_client: TestClient, bad_id: str) -> None:
    r = midi_client.put(f"/api/v1/midi/maps/{bad_id}", json=_doc(id=bad_id))
    assert r.status_code in (404, 422), f"{bad_id} must not install"


# ----- read + remove --------------------------------------------------------


def test_get_unknown_map_is_404_with_a_code(midi_client: TestClient) -> None:
    r = midi_client.get("/api/v1/midi/maps/never-installed")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MIDI_MAP_NOT_FOUND"


def test_delete_removes_the_file_and_a_second_delete_is_404(
    midi_client: TestClient, tmp_path: Path
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    on_disk = tmp_path / "data" / "state" / "controller-maps" / "test-device.json"
    assert midi_client.delete("/api/v1/midi/maps/test-device").status_code == 204
    assert not on_disk.exists()
    assert midi_client.delete("/api/v1/midi/maps/test-device").status_code == 404


def test_a_corrupt_document_on_disk_fails_loudly_rather_than_vanishing(
    midi_client: TestClient, tmp_path: Path
) -> None:
    maps_dir = tmp_path / "data" / "state" / "controller-maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    (maps_dir / "broken.json").write_text("{not json", encoding="utf-8")
    r = midi_client.get("/api/v1/midi/maps")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"
    assert "broken" in r.json()["detail"]["message"]


# ----- catalog search -------------------------------------------------------

# The catalog is the 107-device store under tools/deck-diagrams. Its value to
# onboarding is entirely in the QUALITY column: 13 of those directories hold
# plate-label stubs with invented sequential codes, and importing one would
# poison the registry with fabricated wire data.


def test_catalog_search_finds_a_device_by_model(midi_client: TestClient) -> None:
    r = midi_client.get("/api/v1/midi/catalog", params={"q": "flx10"})
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["devices"]]
    assert "ddj-flx10" in ids


def test_catalog_rows_carry_an_honest_quality_grade(midi_client: TestClient) -> None:
    rows = {row["id"]: row for row in midi_client.get("/api/v1/midi/catalog").json()["devices"]}
    # Parsed from the official Pioneer MIDI message list.
    assert rows["ddj-flx10"]["quality"] == "vendor-pdf"
    # Ingested from the Mixxx GPL mapping, real numbers, community-verified.
    assert rows["ddj-sb2"]["quality"] == "mixxx"
    # Plate labels only. Its PDF IS archived, it was just never table-parsed.
    assert rows["ddj-400"]["quality"] == "bootstrap-stub"


def test_catalog_reports_the_control_count_and_whether_a_plate_layout_exists(
    midi_client: TestClient,
) -> None:
    rows = {row["id"]: row for row in midi_client.get("/api/v1/midi/catalog").json()["devices"]}
    assert rows["ddj-flx10"]["controlCount"] > 0
    assert rows["ddj-flx10"]["hasLayout"] is True


def test_catalog_search_matches_the_brand_too(midi_client: TestClient) -> None:
    ids = [
        row["id"]
        for row in midi_client.get("/api/v1/midi/catalog", params={"q": "reloop"}).json()["devices"]
    ]
    assert any(i.startswith("reloop-") for i in ids)


def test_catalog_search_that_matches_nothing_is_empty_not_an_error(
    midi_client: TestClient,
) -> None:
    r = midi_client.get("/api/v1/midi/catalog", params={"q": "zzz-no-such-controller"})
    assert r.status_code == 200
    assert r.json()["devices"] == []


def test_catalog_counts_are_reported_so_coverage_is_never_guessed(
    midi_client: TestClient,
) -> None:
    body = midi_client.get("/api/v1/midi/catalog").json()
    # Measured Sat 29 Aug 2026: 88 mixxx + 6 vendor-pdf + 13 bootstrap-stub.
    # Re-measured Mon 21 Sep 2026: 14 bootstrap-stub after #3736 added
    # reloop-mixtour-pro, whose midi.json source is prose that does not end in
    # .pdf, so the grader grades it down rather than up (#3744).
    # Re-measured Thu 12 Nov 2026: vendor-pdf 7 on main; bootstrap-stub 15 after
    # the DDJ-FLX10 map (#12638) and xdj-az prose source (#10286).
    assert body["counts"]["bootstrap-stub"] == 15
    assert body["counts"]["vendor-pdf"] == 7
    assert body["counts"]["vendor-pdf"] + body["counts"]["mixxx"] == 95


@pytest.mark.requirement("CTRL-11712")
def test_catalog_without_a_bundled_device_store_is_an_explicit_503(
    midi_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] catalog dir is absent [then] GET /catalog returns 503 with bundled status, [else stop]."""
    from apps.webui.server.routes import midi_maps

    no_store = tmp_path / "no-store"
    monkeypatch.setattr(midi_maps, "_CATALOG_DIR", no_store)
    expected_devices = str(no_store / "devices")
    r = midi_client.get("/api/v1/midi/catalog")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "MIDI_CATALOG_UNAVAILABLE"
    assert detail["status"] == "device store not bundled"
    assert detail["path"] == expected_devices
    assert expected_devices in detail["message"]


# ----- every page action type installs (#10276) ------------------------------

# One binding per action type the page's MidiAction union can emit
# (midi-types.ts), the shapes the XDJ-AZ map uses: a centred relative jog and
# CC buttons for CH CUE and MASTER CUE. The installer refused twelve of these
# until #10276, which stripped a corrected XDJ-AZ map to 119 of 154 controls.
_EVERY_PAGE_ACTION: list[dict[str, Any]] = [
    {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_play_toggle", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 2}, "action": {"type": "deck_cue", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 3}, "action": {"type": "deck_hot_cue", "deck": 1, "slot": "A"}},
    {"source": {"ch": 1, "kind": "note", "id": 4}, "action": {"type": "deck_beat_loop", "deck": 1, "beats": 4}},
    {"source": {"ch": 1, "kind": "note", "id": 27}, "action": {"type": "deck_beat_jump", "deck": 1, "beats": -4}},
    {"source": {"ch": 1, "kind": "note", "id": 5}, "action": {"type": "deck_auto_loop_toggle", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 6}, "action": {"type": "deck_loop_exit", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 7}, "action": {"type": "deck_sync_toggle", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 8}, "action": {"type": "deck_manual_loop_cycle", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 29}, "action": {"type": "deck_loop_in", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 30}, "action": {"type": "deck_loop_out", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 9}, "action": {"type": "deck_loop_scale", "deck": 1, "factor": 2}},
    {"source": {"ch": 1, "kind": "note", "id": 10}, "action": {"type": "deck_key_sync_toggle", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 11}, "action": {"type": "deck_stem_eq_toggle", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 12}, "action": {"type": "deck_key_nudge", "deck": 1, "semitones": -1}},
    {"source": {"ch": 1, "kind": "note", "id": 13}, "action": {"type": "deck_tempo_nudge", "deck": 1, "direction": 1}},
    {"source": {"ch": 1, "kind": "cc", "id": 14}, "relative": True, "relativeCentered": True,
     "action": {"type": "deck_jog", "deck": 1}},
    {"source": {"ch": 1, "kind": "note", "id": 15}, "action": {"type": "controller_pad_mode", "deck": 1, "mode": "hot_cue"}},
    {"source": {"ch": 1, "kind": "note", "id": 16}, "action": {"type": "controller_pad", "deck": 1, "pad": 8, "shifted": True}},
    {"source": {"ch": 1, "kind": "cc", "id": 17}, "action": {"type": "mixer_channel", "deck": 1, "target": "filter"}},
    {"source": {"ch": 1, "kind": "cc", "id": 18}, "action": {"type": "mixer_global", "target": "crossfader"}},
    {"source": {"ch": 1, "kind": "cc", "id": 19}, "action": {"type": "deck_pitch", "deck": 1, "lsbOffset": None}},
    {"source": {"ch": 1, "kind": "cc", "id": 20}, "relative": True, "relativeUnit": True,
     "action": {"type": "browse_encoder"}},
    {"source": {"ch": 1, "kind": "note", "id": 21}, "action": {"type": "browse_load", "deck": 1}},
    {"source": {"ch": 1, "kind": "cc", "id": 22}, "ccButton": True, "action": {"type": "channel_cue", "deck": 1}},
    {"source": {"ch": 1, "kind": "cc", "id": 23}, "action": {"type": "headphone_mix"}},
    {"source": {"ch": 1, "kind": "cc", "id": 24}, "action": {"type": "headphone_level"}},
    {"source": {"ch": 1, "kind": "cc", "id": 25}, "ccButton": True, "action": {"type": "master_cue", "mode": "hold"}},
    {"source": {"ch": 1, "kind": "note", "id": 26}, "action": {"type": "shift_modifier"}},
    {"source": {"ch": 1, "kind": "note", "id": 28}, "action": {"type": "color_fx", "deck": 1, "effect": "filter"}},
    {"source": {"ch": 3, "kind": "note", "id": 114},
     "action": {"type": "mixer_deck_pair_select", "pair": "1-3"}},
]


def test_every_page_action_type_installs_and_reads_back(midi_client: TestClient) -> None:
    bindings = [{**b, "provenance": _cite()} for b in _EVERY_PAGE_ACTION]
    assert len({b["action"]["type"] for b in bindings}) == 31
    r = midi_client.put("/api/v1/midi/maps/test-device", json=_doc(bindings=bindings))
    assert r.status_code == 200, r.text
    back = midi_client.get("/api/v1/midi/maps/test-device").json()["bindings"]
    # Null fields (an absent eq band, a 7-bit pitch lsbOffset) may be spelled
    # or omitted on either side; the action values are what must survive.
    assert [_dense(b["action"]) for b in back] == [_dense(b["action"]) for b in bindings]
    jog = next(b for b in back if b["action"]["type"] == "deck_jog")
    assert jog["relativeCentered"] is True, "the page flag round-trips under its own name"
    assert next(b for b in back if b["action"]["type"] == "master_cue")["ccButton"] is True


def test_unknown_action_type_is_still_refused(midi_client: TestClient) -> None:
    doc = _doc()
    doc["bindings"][0]["action"] = {"type": "deck_scratch", "deck": 1}
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422
    assert "deck_scratch" in r.text


@pytest.mark.parametrize(
    ("binding", "want"),
    [
        ({"source": {"ch": 1, "kind": "note", "id": 9}, "action": {"type": "deck_jog", "deck": 1}}, "cc source"),
        ({"source": {"ch": 1, "kind": "cc", "id": 9}, "action": {"type": "deck_jog", "deck": 1}}, "bound relative"),
        ({"source": {"ch": 1, "kind": "cc", "id": 9}, "action": {"type": "master_cue", "mode": "hold"}}, "note source"),
        ({"source": {"ch": 1, "kind": "note", "id": 9}, "action": {"type": "headphone_mix"}}, "cc or pitchbend"),
    ],
)
def test_page_actions_keep_the_source_rules(
    midi_client: TestClient, binding: dict[str, Any], want: str
) -> None:
    doc = _doc(bindings=[{**binding, "provenance": _cite()}])
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422, r.text
    assert want in r.json()["detail"]["message"]


# ----- feedback output: leds, meters, init (#10058) -------------------------


def _led(**over: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "trigger": {"kind": "deck_playing", "deck": 1},
        "out": {"ch": 1, "note": 11, "velocityOn": 127, "velocityOff": 0},
        "provenance": _cite("vendor-pdf"),
    }
    rule.update(over)
    return rule


def _meter(**over: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "deck": 1,
        "out": {"ch": 1, "cc": 2, "maxValue": 127},
        "provenance": _cite("vendor-pdf"),
    }
    rule.update(over)
    return rule


def _init(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"message": [0xB0, 2, 1], "provenance": _cite("vendor-pdf")}
    entry.update(over)
    return entry


def _put_invalid(client: TestClient, doc: dict[str, Any]) -> str:
    r = client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"
    return str(r.json()["detail"]["message"])


def test_leds_meters_and_init_round_trip_through_put_and_get(
    midi_client: TestClient, tmp_path: Path
) -> None:
    doc = _doc(leds=[_led()], meters=[_meter()], init=[_init()])
    r = midi_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert r.status_code == 200, r.text
    got = midi_client.get("/api/v1/midi/maps/test-device").json()
    assert got["leds"][0]["trigger"] == {"kind": "deck_playing", "deck": 1}
    assert got["leds"][0]["out"] == {"ch": 1, "note": 11, "velocityOn": 127, "velocityOff": 0}
    assert got["leds"][0]["provenance"]["tier"] == "vendor-pdf"
    assert got["meters"] == [_meter()]
    assert got["init"] == [_init()]
    on_disk = json.loads(
        (tmp_path / "data" / "state" / "controller-maps" / "test-device.json").read_text()
    )
    assert on_disk["init"][0]["message"] == [0xB0, 2, 1]


def test_a_document_without_feedback_lists_still_installs_and_reads_back_empty_lists(
    midi_client: TestClient,
) -> None:
    assert midi_client.put("/api/v1/midi/maps/test-device", json=_doc()).status_code == 200
    got = midi_client.get("/api/v1/midi/maps/test-device").json()
    assert (got["leds"], got["meters"], got["init"]) == ([], [], [])


def test_led_rule_with_an_unknown_trigger_kind_is_refused_with_the_rule_index(
    midi_client: TestClient,
) -> None:
    bad = _led(trigger={"kind": "deck_flashing", "deck": 1})
    assert "leds[1]" in _put_invalid(midi_client, _doc(leds=[_led(), bad]))


@pytest.mark.parametrize("ch", [0, 17])
def test_led_rule_out_channel_outside_1_to_16_is_refused_with_the_rule_index(
    midi_client: TestClient, ch: int
) -> None:
    bad = _led(out={"ch": ch, "note": 11, "velocityOn": 127, "velocityOff": 0})
    assert "leds[0].out" in _put_invalid(midi_client, _doc(leds=[bad]))


@pytest.mark.parametrize("field", ["leds", "meters", "init"])
def test_feedback_entry_without_provenance_is_refused(midi_client: TestClient, field: str) -> None:
    entry = {"leds": _led, "meters": _meter, "init": _init}[field]()
    del entry["provenance"]
    assert f"{field}[0]" in _put_invalid(midi_client, _doc(**{field: [entry]}))


@pytest.mark.parametrize(
    "out",
    [
        {"ch": 1, "cc": 2, "maxValue": 0},
        {"ch": 1, "cc": 2, "maxValue": 128},
        {"ch": 0, "cc": 2, "maxValue": 127},
        {"ch": 17, "cc": 2, "maxValue": 127},
    ],
)
def test_meter_out_of_range_is_refused(midi_client: TestClient, out: dict[str, int]) -> None:
    assert "meters[0].out" in _put_invalid(midi_client, _doc(meters=[_meter(out=out)]))


@pytest.mark.parametrize(
    "message",
    [[0xF0, 0, 0], [0x7F, 0, 0], [0xB0, 128, 0], [0xB0, 2], [0xB0, 2, 1, 0]],
)
def test_init_message_must_be_a_channel_voice_triple(
    midi_client: TestClient, message: list[int]
) -> None:
    assert "init[0]" in _put_invalid(midi_client, _doc(init=[_init(message=message)]))


_EVERY_TRIGGER: list[dict[str, Any]] = [
    {"kind": "deck_playing", "deck": 1},
    {"kind": "deck_loaded", "deck": 2},
    {"kind": "loop_engaged", "deck": 1},
    {"kind": "loop_beats_engaged", "deck": 1, "beats": 0.5},
    {"kind": "beat_sync_enabled", "deck": 1},
    {"kind": "beat_sync_effective", "deck": 1},
    {"kind": "stem_eq_enabled", "deck": 1},
    {"kind": "stem_active", "deck": 1, "stem": "drums"},
    {"kind": "stem_solo", "deck": 1, "stem": "vocal"},
    {"kind": "pad_mode_selected", "deck": 1, "mode": "hot_cue"},
    {"kind": "hot_cue_present", "deck": 1, "slot": "A", "padMode": "hot_cue"},
    {"kind": "channel_cue_enabled", "deck": 1},
]


def test_every_runtime_led_trigger_kind_installs(midi_client: TestClient) -> None:
    leds = [
        _led(trigger=t, out={"ch": 1, "note": n, "velocityOn": 127, "velocityOff": 0})
        for n, t in enumerate(_EVERY_TRIGGER)
    ]
    r = midi_client.put("/api/v1/midi/maps/test-device", json=_doc(leds=leds))
    assert r.status_code == 200, r.text
    got = midi_client.get("/api/v1/midi/maps/test-device").json()["leds"]
    assert [rule["trigger"]["kind"] for rule in got] == [t["kind"] for t in _EVERY_TRIGGER]


def test_led_trigger_kinds_match_midi_types_ts() -> None:
    ts = (
        Path(__file__).resolve().parents[2]
        / "apps/webui/frontend/src/lib/rb/midi/midi-types.ts"
    ).read_text(encoding="utf-8")
    block = ts[ts.index("export type LedTrigger =") : ts.index("export interface LedRule")]
    ts_kinds = set(re.findall(r"kind: '(\w+)'", block))
    py_kinds = {
        get_args(model.model_fields["kind"].annotation)[0] for model in _LED_TRIGGER_MODELS
    }
    assert ts_kinds == py_kinds
    assert {t["kind"] for t in _EVERY_TRIGGER} == py_kinds


# ----- runtime union parity and ccButton rules (#10052) ---------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIDI_TYPES_TS = _REPO_ROOT / "apps/webui/frontend/src/lib/rb/midi/midi-types.ts"


def _runtime_action_kinds() -> set[str]:
    # Same pattern as midi-action-parity.test.mjs: one tab, `type: '<kind>';`.
    text = _MIDI_TYPES_TS.read_text(encoding="utf-8")
    return set(re.findall(r"^\ttype: '([a-z_]+)';$", text, re.M))


def _server_action_kinds() -> set[str]:
    union = get_args(MidiActionModel)[0]
    return {get_args(model.model_fields["type"].annotation)[0] for model in get_args(union)}


def _dense(action: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in action.items() if v is not None}


def test_server_action_models_match_the_runtime_union_both_ways() -> None:
    runtime, server = _runtime_action_kinds(), _server_action_kinds()
    assert len(runtime) >= 27, f"parsed too few kinds from midi-types.ts: {sorted(runtime)}"
    assert runtime - server == set(), "runtime kinds the installer refuses: add a pydantic model"
    assert server - runtime == set(), "server kinds the runtime never emits"


def test_every_server_action_kind_has_exactly_one_source_rule() -> None:
    rules = [_BUTTON_ACTIONS, _RELATIVE_ACTIONS, _CONTINUOUS_ACTIONS, frozenset({"deck_pitch"})]
    union = frozenset().union(*rules)
    assert union == _server_action_kinds()
    assert sum(len(r) for r in rules) == len(union), "a kind sits in two source-rule sets"


@pytest.mark.parametrize("binding", _EVERY_PAGE_ACTION, ids=lambda b: b["action"]["type"])
def test_each_page_action_installs_alone_and_reads_back(
    midi_client: TestClient, binding: dict[str, Any]
) -> None:
    r = midi_client.put(
        "/api/v1/midi/maps/test-device", json=_doc(bindings=[{**binding, "provenance": _cite()}])
    )
    assert r.status_code == 200, r.text
    (back,) = midi_client.get("/api/v1/midi/maps/test-device").json()["bindings"]
    assert _dense(back["action"]) == _dense(binding["action"])
    for flag in ("relative", "relativeCentered", "relativeUnit", "ccButton"):
        assert back.get(flag, False) is binding.get(flag, False), flag


@pytest.mark.parametrize(
    ("binding", "want"),
    [
        ({"source": {"ch": 1, "kind": "note", "id": 9}, "ccButton": True,
          "action": {"type": "master_cue", "mode": "hold"}}, "ccButton requires a cc source"),
        ({"source": {"ch": 1, "kind": "pitchbend", "id": 0}, "ccButton": True,
          "action": {"type": "mixer_global", "target": "crossfader"}}, "ccButton requires a cc source"),
        ({"source": {"ch": 1, "kind": "cc", "id": 9}, "ccButton": True,
          "action": {"type": "mixer_channel", "deck": 1, "target": "fader"}},
         "ccButton only applies to a button action"),
        ({"source": {"ch": 1, "kind": "cc", "id": 9}, "ccButton": True, "relative": True,
          "action": {"type": "deck_jog", "deck": 1}}, "ccButton only applies to a button action"),
        ({"source": {"ch": 1, "kind": "cc", "id": 9}, "ccButton": True,
          "action": {"type": "deck_pitch", "deck": 1, "lsbOffset": None}},
         "ccButton only applies to a button action"),
    ],
)
def test_cc_button_rules(midi_client: TestClient, binding: dict[str, Any], want: str) -> None:
    r = midi_client.put(
        "/api/v1/midi/maps/test-device", json=_doc(bindings=[{**binding, "provenance": _cite()}])
    )
    assert r.status_code == 422, r.text
    assert want in r.json()["detail"]["message"]


def test_flags_default_false_when_absent(midi_client: TestClient) -> None:
    assert midi_client.put("/api/v1/midi/maps/test-device", json=_doc()).status_code == 200
    for b in midi_client.get("/api/v1/midi/maps/test-device").json()["bindings"]:
        for flag in ("relativeCentered", "relativeUnit", "ccButton"):
            assert b.get(flag, False) is False, flag


# ----- shipped engine export: Hercules DJ Control Instinct (#13104) -----------

_DEVICE_MAPS_JSON = _REPO_ROOT / "apps/audio-engine/maps/device-maps.json"
_INSTINCT_NAME_MATCH = (
    r"DJ Control Instinct(?:$|\S|\s+(?:$|[^\sP]|P(?:$|[^8])|P8\w))"
)
_INSTINCT_PDF_CITE = (
    "https://ts.hercules.com/download/sound/manuals/DJ_Instinct/QSG/"
    "DJCInstinct_Technical_specifications.pdf MIDI input table"
)
_INSTINCT_EXPORT_BINDING_COUNT = 48


def _instinct_vendor_pdf_provenance() -> dict[str, Any]:
    return {"tier": "vendor-pdf", "cite": _INSTINCT_PDF_CITE, "verified": True}


def _shipped_hercules_instinct_export() -> dict[str, Any]:
    maps = [
        m
        for m in json.loads(_DEVICE_MAPS_JSON.read_text(encoding="utf-8"))["maps"]
        if m.get("nameMatch") == _INSTINCT_NAME_MATCH
    ]
    assert len(maps) == 1, "expected exactly one shipped Instinct export slice"
    return maps[0]


def _installable_doc_from_instinct_export(export: dict[str, Any]) -> dict[str, Any]:
    cite = _instinct_vendor_pdf_provenance()
    bindings: list[dict[str, Any]] = []
    for row in export["bindings"]:
        binding = {k: v for k, v in row.items() if k != "label"}
        binding["provenance"] = cite
        bindings.append(binding)
    leds = [{**rule, "provenance": cite} for rule in export.get("leds", [])]
    init: list[dict[str, Any]] = []
    for entry in export.get("init", []):
        if isinstance(entry, list):
            init.append({"message": entry, "provenance": cite})
        else:
            init.append({**entry, "provenance": cite})
    return {
        "schemaVersion": 1,
        "id": "hercules-dj-control-instinct-verify",
        "vendor": export["vendor"],
        "model": "Hercules DJ Control Instinct",
        "nameMatch": export["nameMatch"],
        "bindings": bindings,
        "leds": leds,
        "init": init,
    }


@pytest.mark.requirement("MIDI-13602")
def test_shipped_hercules_instinct_round_trips_install_api(midi_client: TestClient) -> None:
    """[if] Instinct export is converted to MidiMapDoc [then] PUT and GET succeed, [else stop]."""
    export = _shipped_hercules_instinct_export()
    assert len(export["bindings"]) == _INSTINCT_EXPORT_BINDING_COUNT
    doc = _installable_doc_from_instinct_export(export)
    map_id = doc["id"]
    put = midi_client.put(f"/api/v1/midi/maps/{map_id}", json=doc)
    assert put.status_code == 200, put.text
    got = midi_client.get(f"/api/v1/midi/maps/{map_id}")
    assert got.status_code == 200
    body = got.json()
    assert len(body["bindings"]) == _INSTINCT_EXPORT_BINDING_COUNT
    assert body["nameMatch"] == export["nameMatch"]
    assert body["leds"] and len(body["leds"]) == len(export.get("leds", []))
