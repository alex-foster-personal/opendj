"""FB-22 (pin 49f9d217): a comment pin records who was signed in and a UI config snapshot.

Requirements (mini-PRD):
  - The signed-in user is stamped by the daemon from the session cookie, never
    taken from the request body.
    [if] a signed-out pin carries a user [then] broken
    [if] a signed-in pin carries no user [then] broken
    [if] a body field can set the user [then] broken
  - The UI config snapshot is a closed set of slugs and booleans, so it cannot
    carry a secret, a path under the user's home, or a track title.
    [if] a snapshot with an unknown key is stored [then] broken
    [if] a route under a home directory is stored [then] broken
    [if] a switch value that is not a boolean is stored [then] broken
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.auth import SESSION_COOKIE_NAME, GoogleIdentity, SessionStore

UI_CONFIG = {
    "route": "/performance",
    "app_mode": "performance",
    "engine_mode": "webaudio",
    "perf_tier": "auto",
    "switches": {"show_stems": True, "beat_sync_max": False},
}


def _pin(**extra: object) -> dict[str, object]:
    return {
        "x_pct": 10,
        "y_pct": 20,
        "page": "/performance",
        "text": "pin provenance",
        "ui": "chrome-loop",
        "viewport_width": 1280,
        "viewport_height": 800,
        **extra,
    }


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return path


@pytest.fixture
def fb(tmp_path: Path, state_db_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        bind_host="127.0.0.1",
        hostname="test-host",
        port=18697,
        frontend_port=19411,
        state_db_path=str(state_db_path),
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def _sign_in(client: TestClient, state_db_path: Path) -> None:
    token = SessionStore(state_db_path).sign_in(
        GoogleIdentity(
            google_sub="sub-123",
            email="sub-123@example.com",
            name="Test User",
            avatar_url=None,
            refresh_token="refresh-token-value",
            access_token="access-token-value",
            access_expires_at="2099-01-01T00:00:00+00:00",
        )
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)


@pytest.mark.requirement("FB-22")
def test_pin_49f9d217_signed_out_pin_has_no_user(fb: TestClient) -> None:
    """[if] a signed-out pin carries a user [then] broken, [else stop]."""
    r = fb.post("/api/v1/feedback/comments", json=_pin(ui_config=UI_CONFIG))
    assert r.status_code == 201, r.text
    env = r.json()["environment"]
    assert "user_email" in env, "the field must exist so signed-out reads as an explicit null"
    assert env["user_email"] is None


@pytest.mark.requirement("FB-22")
def test_pin_49f9d217_signed_in_pin_is_stamped_from_the_session(
    fb: TestClient, state_db_path: Path
) -> None:
    """[if] a signed-in pin carries no user [then] broken, [else stop]."""
    _sign_in(fb, state_db_path)
    r = fb.post("/api/v1/feedback/comments", json=_pin(ui_config=UI_CONFIG))
    assert r.status_code == 201, r.text
    assert r.json()["environment"]["user_email"] == "sub-123@example.com"
    stored = fb.get("/api/v1/feedback/comments").json()["comments"][0]
    assert stored["environment"]["user_email"] == "sub-123@example.com"
    assert "token" not in r.text, "no session or OAuth token may reach the pin store"


@pytest.mark.requirement("FB-22")
def test_pin_49f9d217_body_cannot_name_the_user(fb: TestClient) -> None:
    """[if] a body field can set the user [then] broken, [else stop]."""
    r = fb.post(
        "/api/v1/feedback/comments",
        json=_pin(user_email="someone-else@example.com", ui_config=UI_CONFIG),
    )
    assert r.status_code == 201, r.text
    assert r.json()["environment"]["user_email"] is None


@pytest.mark.requirement("FB-22")
def test_pin_49f9d217_ui_config_snapshot_round_trips(fb: TestClient) -> None:
    """[if] a stored pin loses or alters its UI config snapshot [then] broken, [else stop]."""
    r = fb.post("/api/v1/feedback/comments", json=_pin(ui_config=UI_CONFIG))
    assert r.status_code == 201, r.text
    assert r.json()["environment"]["ui_config"] == UI_CONFIG
    stored = fb.get("/api/v1/feedback/comments").json()["comments"][0]
    assert stored["environment"]["ui_config"] == UI_CONFIG


@pytest.mark.requirement("FB-22")
def test_pin_49f9d217_snapshot_is_optional_for_agent_callers(fb: TestClient) -> None:
    """[if] a pin posted with no snapshot is refused or invents one [then] broken, [else stop]."""
    r = fb.post("/api/v1/feedback/comments", json=_pin())
    assert r.status_code == 201, r.text
    assert r.json()["environment"]["ui_config"] is None


@pytest.mark.requirement("FB-22")
@pytest.mark.parametrize(
    ("label", "patch"),
    [
        ("unknown key", {"api_key": "sk-not-a-real-key"}),
        ("home path as route", {"route": "/Users/someone/Music"}),
        ("linux home path as route", {"route": "/home/someone/music"}),
        ("query string in route", {"route": "/performance?token=abc"}),
        ("free text mode", {"app_mode": "Some Track Title"}),
        ("path as engine mode", {"engine_mode": "/Users/someone"}),
        ("string switch value", {"switches": {"show_stems": "a track title"}}),
        ("free text switch key", {"switches": {"Some Track Title": True}}),
        ("too many switches", {"switches": {f"s{i}": True for i in range(40)}}),
    ],
)
def test_pin_49f9d217_snapshot_rejects_anything_but_slugs_and_booleans(
    fb: TestClient, label: str, patch: dict[str, object]
) -> None:
    """[if] a secret, a home path or free text can enter the snapshot [then] broken, [else stop]."""
    r = fb.post("/api/v1/feedback/comments", json=_pin(ui_config={**UI_CONFIG, **patch}))
    assert r.status_code == 422, f"{label}: {r.status_code} {r.text}"
    assert fb.get("/api/v1/feedback/comments").json()["comments"] == [], label
