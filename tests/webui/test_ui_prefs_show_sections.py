"""STRUCT-05 and STRUCTUI-12787 segment display prefs over HTTP (agent parity).

[if] show_sections is PUT over HTTP [then] it persists and defaults off, [else stop].

The "Show sections" toggle on the Phrase row of the analysis-source menu, and
the settings overlay, both drive this one key, so PUT /api/v1/ui-prefs is
the programmatic twin of the checkbox. Regression lines:
- if the key is absent from GET defaults then the overlay boots on nothing
- if the default flips on then every existing user gets bands they never asked for
- if a PUT does not land in ui-prefs.json then the toggle is decorative
- if a non-boolean on disk is coerced rather than refused then a typo turns
  the overlay on
- if a PUT of show_sections clobbers an unrelated topbar boolean (they share a
  merge helper) then turning sections on silently changes beat sync
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _disk(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())


@pytest.mark.requirement("STRUCT-05")
def test_show_sections_defaults_false(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with no stored value [then] show_sections is false, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    body = r.json()
    assert "show_sections" in body, "GET must name the key so the UI can hydrate it"
    assert body["show_sections"] is False


@pytest.mark.requirement("STRUCT-05")
def test_show_sections_put_round_trips_through_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] a PUT names show_sections [then] the reply, the disk blob and a later GET carry it, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_sections": True})
    assert r.status_code == 200
    assert r.json()["show_sections"] is True
    assert _disk(tmp_path)["show_sections"] is True
    assert prefs_client.get("/api/v1/ui-prefs").json()["show_sections"] is True

    # And back off: the overshoot of "turning it on works" is a key that can
    # never be turned off again.
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_sections": False})
    assert r.json()["show_sections"] is False
    assert _disk(tmp_path)["show_sections"] is False


@pytest.mark.requirement("STRUCT-05")
def test_show_sections_put_leaves_other_booleans_alone(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] a PUT names only show_sections [then] beat_sync_max keeps its stored value, [else stop]."""
    assert prefs_client.put("/api/v1/ui-prefs", json={"beat_sync_max": False}).status_code == 200
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_sections": True})
    assert r.json()["beat_sync_max"] is False
    assert r.json()["show_sections"] is True
    # Control, the other direction: a PUT naming only beat_sync_max must not
    # reset show_sections either.
    r = prefs_client.put("/api/v1/ui-prefs", json={"beat_sync_max": True})
    assert r.json()["show_sections"] is True


@pytest.mark.requirement("STRUCT-05")
def test_show_sections_rejects_non_boolean_on_disk(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] show_sections on disk is not a boolean [then] GET returns 422 UI_PREFS_INVALID, [else stop]."""
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"show_sections": "yes"}))
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"
    assert "show_sections" in r.json()["detail"]["message"]


@pytest.mark.requirement("STRUCTUI-12787")
def test_segment_display_defaults_on_fresh_get(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched fresh [then] structui styles default M1/O1/hide, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["sections_main_style"] == "m1_tint"
    assert body["sections_overview_style"] == "o1_strip"
    assert body["sections_uncertain"] == "hide"
    assert body["sections_sticky_title"] is True
    assert body["sections_next_countdown"] is True


@pytest.mark.requirement("STRUCTUI-12787")
def test_put_main_m2_and_overview_o1_persist(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] a PUT sets main style M2 and overview O1 [then] disk and GET carry both, [else stop]."""
    r = prefs_client.put(
        "/api/v1/ui-prefs",
        json={
            "sections_main_style": "m2_recolor",
            "sections_overview_style": "o1_strip",
        },
    )
    assert r.status_code == 200
    assert r.json()["sections_main_style"] == "m2_recolor"
    assert r.json()["sections_overview_style"] == "o1_strip"
    disk = _disk(tmp_path)
    assert disk["sections_main_style"] == "m2_recolor"
    assert disk["sections_overview_style"] == "o1_strip"
    assert prefs_client.get("/api/v1/ui-prefs").json()["sections_uncertain"] == "hide"


@pytest.mark.requirement("STRUCTUI-12787")
def test_show_sections_off_leaves_structui_style_keys_intact(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] Show segments is off [then] stored style keys remain on GET, [else stop]."""
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"show_sections": False, "sections_main_style": "m3_lines"},
    )
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["show_sections"] is False
    assert body["sections_main_style"] == "m3_lines"


@pytest.mark.requirement("STRUCTUI-12787")
def test_uncertain_hide_is_default_and_show_faded_round_trips(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] uncertain is hide [then] default GET says hide and show_faded round-trips, [else stop]."""
    assert prefs_client.get("/api/v1/ui-prefs").json()["sections_uncertain"] == "hide"
    r = prefs_client.put("/api/v1/ui-prefs", json={"sections_uncertain": "show_faded"})
    assert r.json()["sections_uncertain"] == "show_faded"
    assert _disk(tmp_path)["sections_uncertain"] == "show_faded"
