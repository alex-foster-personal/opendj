"""TOPVIEW-01..06: the top-bar view prefs over HTTP (agent parity).

The view icons (list, 4-waveform, phase scope, phase meter), the PAD toggle
and two-deck AutoPlay each write one key here, so PUT /api/v1/ui-prefs is
their programmatic twin. Regression lines:
- a key absent from GET defaults means agents cannot read the live view
- a PUT that does not land on disk means the icon is decorative
- an unknown view or a wrong-typed toggle is refused, never coerced
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


def _on_disk(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())


def test_defaults_are_waveform_view_pads_shown_four_deck_autoplay(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["perf_view"] == "waveform"
    assert body["show_pads"] is True
    assert body["auto_play_two_deck"] is False


@pytest.mark.parametrize("view", ["list", "phase-scope", "phase-meter", "waveform"])
def test_every_view_round_trips_to_disk(prefs_client: TestClient, tmp_path: Path, view: str) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"perf_view": view})
    assert r.status_code == 200
    assert r.json()["perf_view"] == view
    assert _on_disk(tmp_path)["perf_view"] == view
    assert prefs_client.get("/api/v1/ui-prefs").json()["perf_view"] == view


@pytest.mark.parametrize(("key", "value"), [("show_pads", False), ("auto_play_two_deck", True)])
def test_toggles_round_trip_to_disk(prefs_client: TestClient, tmp_path: Path, key: str, value: bool) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={key: value})
    assert r.status_code == 200
    assert r.json()[key] is value
    assert _on_disk(tmp_path)[key] is value


def test_a_view_put_leaves_the_toggles_alone(prefs_client: TestClient) -> None:
    assert prefs_client.put("/api/v1/ui-prefs", json={"show_pads": False}).status_code == 200
    assert prefs_client.put("/api/v1/ui-prefs", json={"perf_view": "list"}).status_code == 200
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["show_pads"] is False
    assert body["perf_view"] == "list"


@pytest.mark.parametrize(
    "payload",
    [
        {"perf_view": "grid"},
        {"show_pads": "maybe"},
        {"auto_play_two_deck": 2},
        # Coercible values plain bool would accept and write (Sol P1 4285228879).
        {"show_pads": "false"},
        {"show_pads": 0},
        {"auto_play_two_deck": "true"},
        {"auto_play_two_deck": 1},
        # An explicit null would otherwise be a silent no-op (Sol P1 4301475288).
        {"show_pads": None},
        {"auto_play_two_deck": None},
        {"perf_view": None},
    ],
)
def test_bad_values_are_refused(prefs_client: TestClient, payload: dict[str, object]) -> None:
    assert prefs_client.put("/api/v1/ui-prefs", json=payload).status_code == 422


@pytest.mark.parametrize("key", ["show_pads", "auto_play_two_deck"])
def test_real_booleans_still_write(prefs_client: TestClient, key: str) -> None:
    # Control, the opposite direction: strictness must not refuse a real bool.
    for value in (True, False):
        r = prefs_client.put("/api/v1/ui-prefs", json={key: value})
        assert r.status_code == 200, r.text
        assert r.json()[key] is value


def test_unknown_view_on_disk_is_refused_not_reset(prefs_client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", "perf_view": "grid"}) + "\n", encoding="utf-8")
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422


def test_a_blob_from_before_these_keys_reads_the_defaults(prefs_client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", "confirm": {}}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert (body["perf_view"], body["show_pads"], body["auto_play_two_deck"]) == ("waveform", True, False)


def test_omitting_a_view_pref_still_leaves_it_unchanged(prefs_client: TestClient) -> None:
    # Control, the opposite direction: refusing null must not refuse a patch
    # that simply does not name these keys.
    assert prefs_client.put("/api/v1/ui-prefs", json={"perf_view": "list", "show_pads": False}).status_code == 200
    r = prefs_client.put("/api/v1/ui-prefs", json={"theme": "dark"})
    assert r.status_code == 200, r.text
    assert (r.json()["perf_view"], r.json()["show_pads"]) == ("list", False)



_CONTRACT = Path(__file__).resolve().parents[2] / "apps" / "webui" / "openapi.json"


def _patch_schema(schema: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = schema["components"]["schemas"]["UiPrefsPatch"]
    return patch


def _patch_property(schema: dict[str, Any], key: str) -> dict[str, Any]:
    props: dict[str, dict[str, Any]] = _patch_schema(schema)["properties"]
    assert key in props, f"UiPrefsPatch has no {key} property"
    return props[key]


def _admits_null(prop: dict[str, Any]) -> bool:
    branches: list[dict[str, Any]] = prop.get("anyOf", [prop])
    return any(b.get("type") == "null" for b in branches)


@pytest.mark.parametrize("key", ["show_pads", "auto_play_two_deck", "perf_view"])
def test_the_contract_does_not_advertise_null_for_view_prefs(key: str) -> None:
    # The server refuses an explicit null for these with 422, so the request
    # contract offers them as omittable but never nullable (Sol P1 4302581642),
    # in the live schema and the checked-in openapi.json alike.
    checked_in: dict[str, Any] = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    for schema in (_app_openapi(), checked_in):
        prop = _patch_property(schema, key)
        assert not _admits_null(prop), f"{key}: {prop}"
        assert key not in _patch_schema(schema).get("required", []), f"{key} must stay omittable"


def test_the_contract_null_probe_sees_a_nullable_pref() -> None:
    # Control: the probe reports null where the contract does carry it, so a
    # pass above is not a probe that never fires.
    assert _admits_null(_patch_property(_app_openapi(), "show_stems"))


def _app_openapi() -> dict[str, Any]:
    from apps.webui.server.routes.ui_prefs import UiPrefsPatch

    return {"components": {"schemas": {"UiPrefsPatch": UiPrefsPatch.model_json_schema()}}}
