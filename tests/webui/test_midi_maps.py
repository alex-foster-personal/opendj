"""Installed controller device maps: install, list, read, remove.

The document these routes carry is the SAME shape the browser registers at
runtime (camelCase, `DeviceMap` in apps/webui/frontend/src/lib/rb/midi/
midi-types.ts) plus per-binding provenance. Design:
specs/controller-onboarding.md section 3.1-3.2.

Regression lines:
  if a document with an action outside the union installs then broken
  if a binding without provenance installs then broken
  if a map id with a path separator escapes the maps dir then broken
  if PUT does not write <data>/state/controller-maps/<id>.json then broken
  if DELETE on an absent id returns 200 then broken
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


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
def test_stem_eq_toggle_validates_on_all_controller_decks(deck: int) -> None:
    from pydantic import TypeAdapter

    from apps.webui.server.routes.midi_maps import MidiActionModel

    action = TypeAdapter(MidiActionModel).validate_python(
        {"type": "deck_stem_eq_toggle", "deck": deck}
    )
    assert action.type == "deck_stem_eq_toggle"
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


def test_replacing_a_map_in_place_does_not_clash_with_its_own_name_match(
    midi_client: TestClient,
) -> None:
    midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    r = midi_client.put("/api/v1/midi/maps/test-device", json=_doc())
    assert r.status_code == 200, r.text


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
    assert body["counts"]["bootstrap-stub"] == 14
    assert body["counts"]["vendor-pdf"] + body["counts"]["mixxx"] == 94
