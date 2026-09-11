"""The six karaoke lyric prefs over HTTP (PR-4 section C, agent parity).

Every lyric UI toggle drives one of these keys, so PUT /api/v1/ui-prefs is the
programmatic twin of the settings overlay. Regression lines:
- if a lyric key is absent from GET defaults then the overlay boots on nothing
- if a default flips off then every lyric surface ships dark for everyone
- if a PUT does not land in ui-prefs.json then the toggle is decorative
- if lyrics_load_strategy accepts an unknown value then the library loader
  reaches a branch it has no code for
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

LYRIC_DEFAULTS: dict[str, Any] = {
    "lyrics_global": True,
    "lyrics_library_col": True,
    "lyrics_hover_scrub": True,
    "lyrics_load_strategy": "hover",
    "lyrics_waveform_overlay": True,
    "lyrics_deck_line": True,
}
LYRIC_WRITES: dict[str, Any] = {
    "lyrics_global": False,
    "lyrics_library_col": False,
    "lyrics_hover_scrub": False,
    "lyrics_load_strategy": "in-view",
    "lyrics_waveform_overlay": False,
    "lyrics_deck_line": False,
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


def test_ui_prefs_defaults_include_the_six_lyric_prefs(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    for key, want in LYRIC_DEFAULTS.items():
        assert body[key] == want, f"{key} default"


@pytest.mark.parametrize("key", sorted(LYRIC_WRITES))
def test_ui_prefs_lyric_key_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path, key: str
) -> None:
    value = LYRIC_WRITES[key]
    r = prefs_client.put("/api/v1/ui-prefs", json={key: value})
    assert r.status_code == 200
    assert r.json()[key] == value
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk[key] == value
    assert prefs_client.get("/api/v1/ui-prefs").json()[key] == value


def test_ui_prefs_lyric_put_leaves_the_other_five_alone(prefs_client: TestClient) -> None:
    assert prefs_client.put("/api/v1/ui-prefs", json={"lyrics_deck_line": False}).status_code == 200
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["lyrics_deck_line"] is False
    for key, want in LYRIC_DEFAULTS.items():
        if key == "lyrics_deck_line":
            continue
        assert body[key] == want, f"{key} was collateral damage"


def test_ui_prefs_rejects_an_unknown_load_strategy(prefs_client: TestClient) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"lyrics_load_strategy": "bogus"})
    assert r.status_code == 422


def test_ui_prefs_rejects_a_non_boolean_lyric_toggle(prefs_client: TestClient) -> None:
    # Pydantic's lax mode reads "yes"/"on"/1 as booleans, the same as every
    # other toggle on this route. Anything outside that vocabulary must be
    # refused rather than land on the model's default.
    r = prefs_client.put("/api/v1/ui-prefs", json={"lyrics_global": "maybe"})
    assert r.status_code == 422


def test_ui_prefs_reads_a_blob_written_before_the_lyric_keys_existed(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", "confirm": {}}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    for key, want in LYRIC_DEFAULTS.items():
        assert body[key] == want, f"{key} must default for a pre-existing blob"


def test_ui_prefs_refuses_a_wrong_typed_lyric_key_on_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lyrics_global": "yes"}) + "\n", encoding="utf-8")
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422
