"""OBS-05 / OBS-06: the consent route the first-launch dialog talks to.

[if] a fresh install asks for its decision [then] it reads undecided, current terms, [else stop].
[if] the dialog records accepted [then] the file is written and the send gate opens, [else stop].
[if] the dialog records declined [then] the gate closes and the answer is remembered, [else stop].
[if] acceptance names stale terms [then] the route answers 409 and writes nothing, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.telemetry.consent import (
    TERMS_VERSION,
    held_for_consent,
    reset_consent_for_tests,
)
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("OBS-05")

_LOOPBACK = "http://127.0.0.1"
FRONTEND_DSN = "https://public@o0.ingest.de.sentry.io/43"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reset_consent_for_tests()
    monkeypatch.delenv("SENTRY_FRONTEND_DSN", raising=False)
    monkeypatch.delenv("OPENDJ_BUNDLED_TELEMETRY", raising=False)
    app = create_app(backend=InMemoryBackend(), mount_frontend=False, enable_cors=False)
    app.state.telemetry_consent_dir = tmp_path
    with TestClient(app, base_url=_LOOPBACK) as c:
        yield c
    reset_consent_for_tests()


def test_fresh_install_is_undecided_and_names_the_current_terms(client: TestClient) -> None:
    body = client.get("/api/v1/telemetry/consent").json()
    assert body["decision"] == "undecided"
    assert body["terms_current_version"] == TERMS_VERSION
    assert body["terms_version"] is None
    assert body["replay_loader_url"] is None, "no frontend DSN means no replay"
    assert body["telemetry_active"] is False, "the test app has no SDK client"


def test_accepting_writes_the_file_and_opens_the_gate(client: TestClient, tmp_path: Path) -> None:
    assert held_for_consent() is True
    response = client.put(
        "/api/v1/telemetry/consent",
        json={"decision": "accepted", "terms_version": TERMS_VERSION},
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "accepted"
    assert held_for_consent() is False
    stored = json.loads((tmp_path / "telemetry-consent.json").read_text(encoding="utf-8"))
    assert stored["decision"] == "accepted"
    assert client.get("/api/v1/telemetry/consent").json()["decision"] == "accepted"


def test_declining_is_remembered_and_closes_the_gate(client: TestClient) -> None:
    client.put(
        "/api/v1/telemetry/consent",
        json={"decision": "accepted", "terms_version": TERMS_VERSION},
    )
    response = client.put(
        "/api/v1/telemetry/consent",
        json={"decision": "declined", "terms_version": TERMS_VERSION},
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "declined"
    assert held_for_consent() is True


def test_accepting_stale_terms_is_a_409(client: TestClient) -> None:
    response = client.put(
        "/api/v1/telemetry/consent",
        json={"decision": "accepted", "terms_version": "1999-01-01"},
    )
    assert response.status_code == 409
    assert TERMS_VERSION in response.json()["detail"]
    assert client.get("/api/v1/telemetry/consent").json()["decision"] == "undecided"


def test_frontend_dsn_in_env_yields_a_replay_loader_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTRY_FRONTEND_DSN", FRONTEND_DSN)
    body = client.get("/api/v1/telemetry/consent").json()
    assert body["replay_loader_url"] == "https://js-de.sentry-cdn.com/public.min.js"
    assert body["replay_session_sample_rate"] == 1.0
    assert body["replay_on_error_sample_rate"] == 1.0
