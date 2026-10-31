"""UIURL-01: the request guard admits exactly opendj.localhost beside loopback.

[if] Host is opendj.localhost [then] served; any other *.localhost is 403, [else stop].
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.local_operator import is_loopback_hostname

pytestmark = pytest.mark.requirement("UIURL-01")

BACKEND_PORT = 8697
FRONTEND_PORT = 9411
COMMAND = {"single": {"type": "play", "deck": 1, "playing": True}}


def _client() -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=True,
        port=BACKEND_PORT,
        frontend_port=FRONTEND_PORT,
    )
    return TestClient(app, base_url="http://127.0.0.1")


def _guard_refused(resp) -> bool:
    return resp.status_code == 403 and resp.json().get("code") in {
        "HOST_NOT_ALLOWED",
        "ORIGIN_NOT_ALLOWED",
    }


def test_named_local_host_is_served() -> None:
    resp = _client().get("/api/v1/health", headers={"Host": f"opendj.localhost:{BACKEND_PORT}"})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "host",
    [
        "evil.localhost",  # no *.localhost wildcard
        "opendj.localhost.evil.example",  # suffix lookalike
        "xopendj.localhost",  # prefix lookalike
        "random-host.example",
    ],
)
def test_any_other_host_is_still_refused(host: str) -> None:
    resp = _client().get("/api/v1/health", headers={"Host": f"{host}:{BACKEND_PORT}"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize("port", [BACKEND_PORT, FRONTEND_PORT])
def test_named_origin_on_an_engine_port_may_mutate(port: int) -> None:
    resp = _client().post(
        "/api/v1/commands",
        json=COMMAND,
        headers={"Host": f"opendj.localhost:{BACKEND_PORT}", "Origin": f"http://opendj.localhost:{port}"},
    )
    assert not _guard_refused(resp), resp.text


def test_named_origin_on_a_foreign_port_is_refused() -> None:
    resp = _client().post(
        "/api/v1/commands",
        json=COMMAND,
        headers={"Origin": "http://opendj.localhost:1234"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ORIGIN_NOT_ALLOWED"


def test_local_operator_counts_the_named_host_as_loopback_and_nothing_wider() -> None:
    assert is_loopback_hostname("opendj.localhost")
    assert not is_loopback_hostname("evil.localhost")
    assert not is_loopback_hostname("opendj.localhost.evil.example")
