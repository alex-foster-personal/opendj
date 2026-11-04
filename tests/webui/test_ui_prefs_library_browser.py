"""Issue #2854: library browser prefs, wheel sensitivity, MIDI enabled over HTTP.

Every control in this set drives one of these keys, so PUT /api/v1/ui-prefs is
the programmatic twin. Regression lines:
- if a key is absent from GET defaults then agents cannot read the live value
- if a PUT does not land in ui-prefs.json then the toggle is decorative
- if a wrong-typed key is on disk then GET refuses rather than coercing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

LIBRARY_BROWSER_DEFAULTS: dict[str, Any] = {
    "hide_broken_links": False,
    "library_density": "compact",
    "key_notation": "camelot_musical",
    "next_only_filter": False,
    "remixes_filter": False,
    "vocals_filter": False,
    "available_offline_filter": False,
    "wheel_sensitivity": {"mouse": 1.0, "trackpad": 1.0 / 3.0},
    "midi_enabled": False,
    "spotify_shrink_main_for_player": True,
    "library_key_filter": [],
}
LIBRARY_BROWSER_WRITES: dict[str, Any] = {
    "hide_broken_links": True,
    "library_density": "cosy",
    "key_notation": "musical",
    "next_only_filter": True,
    "remixes_filter": True,
    "vocals_filter": True,
    "available_offline_filter": True,
    "midi_enabled": True,
    "spotify_shrink_main_for_player": False,
    "library_key_filter": ["8A", "9A"],
}


@pytest.fixture
def prefs_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def test_ui_prefs_defaults_include_library_browser_prefs(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    for key, want in LIBRARY_BROWSER_DEFAULTS.items():
        assert body[key] == want, key


@pytest.mark.parametrize("key", tuple(LIBRARY_BROWSER_WRITES))
def test_ui_prefs_library_browser_key_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path, key: str
) -> None:
    value = LIBRARY_BROWSER_WRITES[key]
    r = prefs_client.put("/api/v1/ui-prefs", json={key: value})
    assert r.status_code == 200
    assert r.json()[key] == value
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk[key] == value
    assert prefs_client.get("/api/v1/ui-prefs").json()[key] == value


def test_ui_prefs_wheel_sensitivity_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    payload = {"mouse": 2.0, "trackpad": 0.25}
    r = prefs_client.put("/api/v1/ui-prefs", json={"wheel_sensitivity": payload})
    assert r.status_code == 200
    assert r.json()["wheel_sensitivity"] == payload
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["wheel_sensitivity"] == payload


def test_ui_prefs_wheel_sensitivity_partial_put_merges_trackpad_default(
    prefs_client: TestClient,
) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"wheel_sensitivity": {"mouse": 1.5}})
    assert r.status_code == 200
    body = r.json()["wheel_sensitivity"]
    assert body["mouse"] == 1.5
    assert body["trackpad"] == LIBRARY_BROWSER_DEFAULTS["wheel_sensitivity"]["trackpad"]


@pytest.mark.requirement("LIBM-129")
def test_watcher_folders_validate_rejects_missing_dir(prefs_client: TestClient) -> None:
    """[if] a watcher path is not a directory [then] validate returns 400, [else stop]."""
    r = prefs_client.post(
        "/api/v1/ui-prefs/watcher-folders:validate",
        json={"paths": ["/definitely/not/a/real/music-dj-tools-watcher-dir"]},
    )
    assert r.status_code == 400
    body = r.json()
    assert "missing" in body["detail"]


@pytest.mark.requirement("LIBM-129")
def test_watcher_folders_validate_accepts_existing_dir(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] a watcher path is an existing directory [then] validate returns ok, [else stop]."""
    watch = tmp_path / "watch-me"
    watch.mkdir()
    r = prefs_client.post(
        "/api/v1/ui-prefs/watcher-folders:validate",
        json={"paths": [str(watch)]},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_ui_prefs_library_browser_put_leaves_siblings_alone(prefs_client: TestClient) -> None:
    assert prefs_client.put("/api/v1/ui-prefs", json={"hide_broken_links": True}).status_code == 200
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["hide_broken_links"] is True
    assert body["library_density"] == LIBRARY_BROWSER_DEFAULTS["library_density"]
    assert body["remixes_filter"] is LIBRARY_BROWSER_DEFAULTS["remixes_filter"]
    assert body["midi_enabled"] is LIBRARY_BROWSER_DEFAULTS["midi_enabled"]


def test_ui_prefs_rejects_invalid_library_density(prefs_client: TestClient) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_density": "wide"})
    assert r.status_code == 422


@pytest.mark.parametrize("notation", ("camelot_musical", "camelot", "open_key", "musical"))
def test_ui_prefs_key_notation_round_trips_every_notation(prefs_client: TestClient, notation: str) -> None:
    """[if] a valid key_notation is PUT [then] GET returns it unchanged."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"key_notation": notation})
    assert r.status_code == 200, r.text
    assert prefs_client.get("/api/v1/ui-prefs").json()["key_notation"] == notation


def test_ui_prefs_key_notation_defaults_to_camelot_plus_musical(prefs_client: TestClient) -> None:
    """[if] nothing was ever stored [then] key_notation is camelot_musical (8A \u2022 Am)."""
    assert prefs_client.get("/api/v1/ui-prefs").json()["key_notation"] == "camelot_musical"


@pytest.mark.parametrize("bad", ("traktor", "Camelot", "", 1))
def test_ui_prefs_rejects_unknown_key_notation(prefs_client: TestClient, bad: Any) -> None:
    """One key notation for every row: an unknown one is refused, never coerced."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"key_notation": bad})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "key",
    (
        "hide_broken_links",
        "next_only_filter",
        "remixes_filter",
        "vocals_filter",
        "midi_enabled",
    ),
)
def test_ui_prefs_rejects_non_boolean_library_browser_toggle(
    prefs_client: TestClient, key: str
) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={key: "maybe"})
    assert r.status_code == 422


def test_ui_prefs_rejects_wheel_sensitivity_out_of_range(prefs_client: TestClient) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"wheel_sensitivity": {"mouse": 99}})
    assert r.status_code == 422


def test_ui_prefs_defaults_include_empty_library_watcher_folders(
    prefs_client: TestClient,
) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["library_watcher_folders"] == []


def test_ui_prefs_library_key_filter_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    payload = ["8A", "9A"]
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_key_filter": payload})
    assert r.status_code == 200
    assert r.json()["library_key_filter"] == payload
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_key_filter"] == payload


@pytest.mark.parametrize(
    "bad",
    (
        ["8a"],
        ["13A"],
        ["8A", "8A"],
        "8A",
        ["Am"],
        None,
    ),
)
def test_ui_prefs_rejects_invalid_library_key_filter(
    prefs_client: TestClient, bad: Any
) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_key_filter": bad})
    assert r.status_code == 422


def _key_filter_action(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/api/v1/ui-prefs/library-key-filter", json=body)


def test_library_key_filter_actions_mirror_the_wheel_controls(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """Each wheel control has an HTTP twin (#7599 agent parity): toggle, Compatible, Reset."""
    r = _key_filter_action(prefs_client, {"action": "toggle", "key": "9A"})
    assert r.status_code == 200, r.text
    assert r.json() == {"library_key_filter": ["9A"]}
    r = _key_filter_action(prefs_client, {"action": "toggle", "key": "8A"})
    assert r.json() == {"library_key_filter": ["8A", "9A"]}
    r = _key_filter_action(prefs_client, {"action": "toggle", "key": "9A"})
    assert r.json() == {"library_key_filter": ["8A"]}
    r = _key_filter_action(prefs_client, {"action": "compatible", "reference": "8A"})
    assert r.json() == {"library_key_filter": ["7A", "8B", "8A", "9A"]}
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_key_filter"] == ["7A", "8B", "8A", "9A"]
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_key_filter"] == ["7A", "8B", "8A", "9A"]
    r = _key_filter_action(prefs_client, {"action": "reset"})
    assert r.json() == {"library_key_filter": []}
    r = _key_filter_action(prefs_client, {"action": "set", "keys": ["12A", "1A"]})
    assert r.json() == {"library_key_filter": ["1A", "12A"]}


@pytest.mark.parametrize(
    ("reference", "expected"),
    (
        ("12A", ["1A", "11A", "12B", "12A"]),
        ("1B", ["1B", "1A", "2B", "12B"]),
    ),
)
def test_library_key_filter_compatible_wraps_the_wheel(
    prefs_client: TestClient, reference: str, expected: list[str]
) -> None:
    r = _key_filter_action(prefs_client, {"action": "compatible", "reference": reference})
    assert r.json() == {"library_key_filter": expected}


@pytest.mark.parametrize(
    "body",
    (
        {"action": "toggle"},
        {"action": "toggle", "key": "8a"},
        {"action": "toggle", "key": "Am"},
        {"action": "compatible"},
        {"action": "compatible", "reference": "13B"},
        {"action": "reset", "key": "8A"},
        {"action": "set", "keys": ["8A", "8A"]},
        {"action": "spin"},
    ),
)
def test_library_key_filter_action_rejects_bad_input(
    prefs_client: TestClient, body: dict[str, Any]
) -> None:
    r = _key_filter_action(prefs_client, body)
    assert r.status_code == 422, r.text


@pytest.mark.requirement("LIBM-129")
def test_ui_prefs_library_watcher_folders_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] watcher folders are PUT [then] they persist in ui-prefs.json, [else stop]."""
    paths = ["/Users/dev/watch-a", "/Users/dev/watch-b"]
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_watcher_folders": paths})
    assert r.status_code == 200
    assert r.json()["library_watcher_folders"] == paths
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_watcher_folders"] == paths
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_watcher_folders"] == paths


@pytest.mark.requirement("LIBM-129")
def test_ui_prefs_library_watcher_folders_put_preserves_sibling_prefs(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] watcher folders are PUT [then] unrelated prefs remain, [else stop]."""
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "theme": "light",
                "hide_broken_links": True,
                "library_watcher_folders": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths = ["/Users/dev/watch-a"]
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_watcher_folders": paths})
    assert r.status_code == 200
    body = r.json()
    assert body["library_watcher_folders"] == paths
    assert body["theme"] == "light"
    assert body["hide_broken_links"] is True
    on_disk = json.loads(path.read_text())
    assert on_disk["library_watcher_folders"] == paths
    assert on_disk["theme"] == "light"
    assert on_disk["hide_broken_links"] is True


@pytest.mark.requirement("LIBM-129")
def test_ui_prefs_library_watcher_folders_clear_round_trips(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] watcher folders are cleared [then] GET returns [], [else stop]."""
    prefs_client.put(
        "/api/v1/ui-prefs", json={"library_watcher_folders": ["/Users/dev/watch"]}
    )
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_watcher_folders": []})
    assert r.status_code == 200
    assert r.json()["library_watcher_folders"] == []
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_watcher_folders"] == []


def test_ui_prefs_confirm_null_deletes_playlist_drop_mode_on_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "confirm": {"playlist_drop_mode": "add", "delete_playlist": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = prefs_client.put(
        "/api/v1/ui-prefs", json={"confirm": {"playlist_drop_mode": None}}
    )
    assert r.status_code == 200
    body = r.json()
    assert "playlist_drop_mode" not in body["confirm"]
    assert body["confirm"]["delete_playlist"] is False
    on_disk = json.loads(path.read_text())
    assert "playlist_drop_mode" not in on_disk["confirm"]
    assert on_disk["confirm"]["delete_playlist"] is False
    assert prefs_client.get("/api/v1/ui-prefs").json()["confirm"] == {
        "delete_playlist": False
    }


def test_ui_prefs_rejects_malformed_library_watcher_folders_on_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"theme": "dark", "library_watcher_folders": "not-a-list"}) + "\n",
        encoding="utf-8",
    )
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422


def test_ui_prefs_reads_a_blob_written_before_the_library_browser_keys_existed(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", "confirm": {}}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    for key, want in LIBRARY_BROWSER_DEFAULTS.items():
        assert body[key] == want, key


@pytest.mark.parametrize("key", ("hide_broken_links", "library_density", "midi_enabled"))
def test_ui_prefs_refuses_a_wrong_typed_library_browser_key_on_disk(
    prefs_client: TestClient, tmp_path: Path, key: str
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", key: "yes"}) + "\n", encoding="utf-8")
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422


COMPATIBLE_FILTER_DEFAULTS: dict[str, Any] = {
    "rules": ["same_key", "adjacent", "relative"],
    "bpm_window_bpm": 20.0,
    "bpm_enabled": True,
    "allow_half_double": True,
    "bpm_direction": "both",
    "mode": "transition",
    "key_shift_max": 1,
    "mashup_relative": False,
}


def test_ui_prefs_defaults_include_compatible_filter(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["compatible_filter"] == COMPATIBLE_FILTER_DEFAULTS


@pytest.mark.requirement("LIBUX-32")
def test_ui_prefs_compatible_filter_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] compatible-filter ranges are PUT [then] a fresh GET and ui-prefs.json return them, [else stop]."""
    chosen = {
        "rules": ["same_key", "diagonal", "jaws"],
        "bpm_window_bpm": 10,
        "bpm_enabled": True,
        "allow_half_double": False,
        "bpm_direction": "above",
    }
    r = prefs_client.put("/api/v1/ui-prefs", json={"compatible_filter": chosen})
    assert r.status_code == 200
    expected = {**COMPATIBLE_FILTER_DEFAULTS, **chosen, "bpm_window_bpm": 10.0}
    assert r.json()["compatible_filter"] == expected
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["compatible_filter"] == expected
    assert prefs_client.get("/api/v1/ui-prefs").json()["compatible_filter"] == expected


@pytest.mark.requirement("LIBUX-32")
def test_ui_prefs_compatible_filter_reloads_from_disk_in_a_new_app(
    tmp_path: Path,
) -> None:
    """[if] the engine restarts [then] GET serves the compatible filter written before, [else stop]."""
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()

    def _client() -> TestClient:
        app = create_app(
            backend=SqliteBackend(state_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            mount_frontend=False,
            state_db_path=str(state_path),
        )
        app.state.data_dir = data_dir
        return TestClient(app)

    with _client() as first:
        r = first.put(
            "/api/v1/ui-prefs",
            json={"compatible_filter": {"rules": ["relative"], "bpm_direction": "same"}},
        )
        assert r.status_code == 200
    with _client() as second:
        body = second.get("/api/v1/ui-prefs").json()["compatible_filter"]
    assert body == {**COMPATIBLE_FILTER_DEFAULTS, "rules": ["relative"], "bpm_direction": "same"}


def test_ui_prefs_compatible_filter_partial_put_keeps_stored_fields(
    prefs_client: TestClient,
) -> None:
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"compatible_filter": {"bpm_window_bpm": 10, "bpm_direction": "below"}},
    )
    r = prefs_client.put("/api/v1/ui-prefs", json={"compatible_filter": {"bpm_enabled": False}})
    assert r.status_code == 200
    assert r.json()["compatible_filter"] == {
        **COMPATIBLE_FILTER_DEFAULTS,
        "bpm_window_bpm": 10.0,
        "bpm_direction": "below",
        "bpm_enabled": False,
    }
    # Sibling prefs are untouched by a compatible_filter PUT.
    assert r.json()["library_density"] == LIBRARY_BROWSER_DEFAULTS["library_density"]


@pytest.mark.parametrize(
    "bad",
    (
        {"rules": ["same_key", "tritone"]},
        {"rules": ["same_key", "same_key"]},
        {"rules": "same_key"},
        {"bpm_window_bpm": -1},
        {"bpm_direction": "sideways"},
        {"mode": "blend"},
        {"key_shift_max": 4},
        {"key_shift_max": 1.0},
        {"key_shift_max": True},
        {"key_shift_max": "1"},
    ),
)
def test_ui_prefs_rejects_invalid_compatible_filter(
    prefs_client: TestClient, bad: dict[str, Any]
) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"compatible_filter": bad})
    assert r.status_code == 422


@pytest.mark.requirement("KEYSHIFT-01")
def test_ui_prefs_compatible_filter_modes_round_trip(prefs_client: TestClient) -> None:
    """[if] a mode, key shift and relative flags are PUT [then] GET returns them, [else stop]."""
    chosen = {
        "mode": "mashup",
        "key_shift_max": 3,
        "rules": ["same_key"],
        "mashup_relative": True,
    }
    r = prefs_client.put("/api/v1/ui-prefs", json={"compatible_filter": chosen})
    assert r.status_code == 200
    assert prefs_client.get("/api/v1/ui-prefs").json()["compatible_filter"] == {
        **COMPATIBLE_FILTER_DEFAULTS,
        **chosen,
    }


@pytest.mark.parametrize(
    "stored",
    ({"bpm_enabled": "yes"}, {"camelot_steps": []}, {"camelot_steps": {}}, {"camelot_steps": "1"}, {"camelot_steps": 3}),
)
def test_ui_prefs_get_refuses_wrong_typed_compatible_filter_on_disk(
    prefs_client: TestClient, tmp_path: Path, stored: dict[str, Any]
) -> None:
    """[if] a stored filter field (legacy camelot_steps too) has the wrong type [then] GET is a 422, never a 500."""
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"compatible_filter": stored}) + "\n", encoding="utf-8")
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("steps", "rules"),
    (
        (0, ["same_key", "relative"]),
        (1, ["same_key", "adjacent", "relative"]),
        (2, ["same_key", "adjacent", "relative", "energy_boost", "energy_drop"]),
    ),
)
def test_ui_prefs_migrates_legacy_camelot_steps_to_rules(
    prefs_client: TestClient, tmp_path: Path, steps: int, rules: list[str]
) -> None:
    """[if] ui-prefs.json predates rule checkboxes [then] camelot_steps reads as its nearest rules."""
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"compatible_filter": {"camelot_steps": steps}}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()["compatible_filter"]
    assert body == {**COMPATIBLE_FILTER_DEFAULTS, "rules": rules}


def test_ui_prefs_unknown_camelot_rule_names_the_known_ones(prefs_client: TestClient) -> None:
    """[if] a PUT names an unknown rule [then] the 422 lists every valid rule id."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"compatible_filter": {"rules": ["tritone"]}})
    assert r.status_code == 422
    assert "same_key|adjacent|relative|diagonal" in json.dumps(r.json())


def test_ui_prefs_empty_camelot_rules_is_kept(prefs_client: TestClient) -> None:
    """[if] every rule box is unticked [then] the empty list is stored, not reset to defaults."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"compatible_filter": {"rules": []}})
    assert r.status_code == 200
    assert prefs_client.get("/api/v1/ui-prefs").json()["compatible_filter"]["rules"] == []
