"""MIDI Learn save route (#9781): POST /api/v1/midi/maps/{id}/learned-binding.

Agent parity for the MIDI panel's Learn button: the same save the UI makes is
reachable over HTTP, so an agent can bind a control without the panel.

Regression lines:
  if a learned binding is not appended to the user map with tier 'learned' then broken
  if a learned control already bound to another command saves without replace then broken
  if replace does not rebind the control in place then broken
  if a missing map with no template is created then broken
  if a template does not seed a user map copy under the path id then broken
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

# requirement: CTRL-9781


@pytest.fixture
def midi_client(tmp_path: Path) -> Iterator[TestClient]:
    """App with an isolated data dir so installed maps land under tmp_path."""
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


def _doc() -> dict[str, Any]:
    """A one-binding user map: note 11 plays deck 1."""
    return {
        "schemaVersion": 1,
        "id": "user-testdevice",
        "vendor": "TestCo",
        "model": "TestDevice 1",
        "nameMatch": "TestDevice",
        "bindings": [
            {
                "source": {"ch": 1, "kind": "note", "id": 11},
                "action": {"type": "deck_play_toggle", "deck": 1},
                "provenance": {"tier": "hand", "cite": "test fixture", "verified": False},
            }
        ],
    }


URL = "/api/v1/midi/maps/user-testdevice/learned-binding"


def test_learned_binding_is_appended_with_learned_provenance(midi_client: TestClient) -> None:
    """A new control is added to the installed user map and persisted."""
    assert midi_client.put("/api/v1/midi/maps/user-testdevice", json=_doc()).status_code == 200
    binding = {"source": {"ch": 1, "kind": "note", "id": 12}, "action": {"type": "deck_cue", "deck": 1}}
    r = midi_client.post(URL, json={"binding": binding})
    assert r.status_code == 200, r.text
    got = midi_client.get("/api/v1/midi/maps/user-testdevice").json()
    assert len(got["bindings"]) == 2
    assert got["bindings"][1]["source"] == {"ch": 1, "kind": "note", "id": 12}
    assert got["bindings"][1]["action"] == {"type": "deck_cue", "deck": 1}
    assert got["bindings"][1]["provenance"]["tier"] == "learned"


def test_conflict_asks_then_replace_rebinds_in_place(midi_client: TestClient) -> None:
    """A control already bound to another command 409s until replace is set."""
    assert midi_client.put("/api/v1/midi/maps/user-testdevice", json=_doc()).status_code == 200
    binding = {"source": {"ch": 1, "kind": "note", "id": 11}, "action": {"type": "deck_cue", "deck": 2}}
    r = midi_client.post(URL, json={"binding": binding})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MIDI_LEARN_CONFLICT"
    assert r.json()["detail"]["existing"]["action"] == {"type": "deck_play_toggle", "deck": 1}
    unchanged = midi_client.get("/api/v1/midi/maps/user-testdevice").json()
    assert unchanged["bindings"][0]["action"]["type"] == "deck_play_toggle"

    r = midi_client.post(URL, json={"binding": binding, "replace": True})
    assert r.status_code == 200, r.text
    got = midi_client.get("/api/v1/midi/maps/user-testdevice").json()
    assert len(got["bindings"]) == 1
    assert got["bindings"][0]["action"] == {"type": "deck_cue", "deck": 2}
    assert got["bindings"][0]["provenance"]["tier"] == "learned"


def test_missing_map_without_template_is_404(midi_client: TestClient) -> None:
    """No user map and no template: nothing is created."""
    binding = {"source": {"ch": 1, "kind": "note", "id": 12}, "action": {"type": "deck_cue", "deck": 1}}
    r = midi_client.post(URL, json={"binding": binding})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MIDI_MAP_NOT_FOUND"


def test_template_seeds_a_user_copy_under_the_path_id(midi_client: TestClient) -> None:
    """A built-in copy sent as template is written as the user map, id forced to the path."""
    template = {**_doc(), "id": "ignored"}
    binding = {"source": {"ch": 2, "kind": "cc", "id": 7}, "action": {"type": "browse_encoder"}, "relative": True}
    r = midi_client.post(URL, json={"binding": binding, "template": template})
    assert r.status_code == 200, r.text
    got = midi_client.get("/api/v1/midi/maps/user-testdevice").json()
    assert got["id"] == "user-testdevice"
    assert [b["provenance"]["tier"] for b in got["bindings"]] == ["hand", "learned"]
    assert got["bindings"][1]["relative"] is True


def test_invalid_learned_binding_is_422(midi_client: TestClient) -> None:
    """A binding outside the action union is refused with the map-invalid code."""
    assert midi_client.put("/api/v1/midi/maps/user-testdevice", json=_doc()).status_code == 200
    binding = {"source": {"ch": 1, "kind": "note", "id": 12}, "action": {"type": "not_a_command"}}
    r = midi_client.post(URL, json={"binding": binding})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MIDI_MAP_INVALID"
