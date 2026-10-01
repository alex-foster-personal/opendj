"""App-usage telemetry: can the engine answer "is the app open" by itself?

Real FastAPI test client throughout -- no mocked engine. The only injected
seam is the monotonic clock, so the 45s in-use boundary is asserted without
any test sleeping through it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.usage_telemetry import IN_USE_WINDOW_SECONDS, UsageStore

# Real user agents. The shell's was captured on Wed 19 Aug 2026 from the
# installed Open DJ.app 0.1.0 pointed at a loopback probe engine.
SHELL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko)"
)
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
CURL_UA = "curl/8.7.1"


class FakeClock:
    """Monotonic seconds under test control."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _client(store: UsageStore | None = None) -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        usage_store=store,
    )
    return TestClient(app)


def _heartbeat(
    client: TestClient,
    *,
    client_id: str = "client-a",
    surface: str = "desktop-shell",
    page_visible: bool = True,
    app_version: str = "0.1.0",
):
    return client.post(
        "/api/v1/telemetry/heartbeat",
        json={
            "client_id": client_id,
            "surface": surface,
            "page_visible": page_visible,
            "app_version": app_version,
        },
    )


# ----------------------------------------------------------- heartbeat


def test_heartbeat_upserts_one_row_per_client_id() -> None:
    with _client() as client:
        first = _heartbeat(client, page_visible=True, app_version="0.1.0")
        second = _heartbeat(client, page_visible=False, app_version="0.2.0")
        assert first.status_code == 200
        assert second.status_code == 200

        clients = client.get("/api/v1/telemetry/clients").json()["clients"]

    assert len(clients) == 1
    assert clients[0]["client_id"] == "client-a"
    assert clients[0]["page_visible"] is False
    assert clients[0]["app_version"] == "0.2.0"


def test_heartbeat_is_server_stamped_with_a_wall_clock_time() -> None:
    # The monotonic clock is frozen so seconds_since_seen is exactly 0.0; the
    # real one advanced 2 ms between heartbeat and read on a loaded CI host.
    with _client(UsageStore(monotonic=FakeClock())) as client:
        body = _heartbeat(client).json()

    assert body["last_seen_at"].endswith("Z")
    assert body["seconds_since_seen"] == 0.0
    assert body["in_use"] is True


def test_unknown_surface_is_refused_rather_than_coerced() -> None:
    with _client() as client:
        response = _heartbeat(client, surface="kiosk")
        assert response.status_code == 422
        assert client.get("/api/v1/telemetry/clients").json()["clients"] == []


def test_two_surfaces_are_listed_separately() -> None:
    with _client() as client:
        _heartbeat(client, client_id="shell-1", surface="desktop-shell")
        _heartbeat(client, client_id="tab-1", surface="browser")
        payload = client.get("/api/v1/telemetry/clients").json()

    surfaces = {row["client_id"]: row["surface"] for row in payload["clients"]}
    assert surfaces == {"shell-1": "desktop-shell", "tab-1": "browser"}


# ----------------------------------------------------------- in_use math


def test_in_use_flips_at_the_45s_boundary() -> None:
    clock = FakeClock()
    store = UsageStore(monotonic=clock)
    with _client(store) as client:
        _heartbeat(client)

        clock.advance(IN_USE_WINDOW_SECONDS - 1.0)
        inside = client.get("/api/v1/telemetry/clients").json()
        clock.advance(2.0)
        outside = client.get("/api/v1/telemetry/clients").json()

    assert inside["clients"][0]["seconds_since_seen"] == 44.0
    assert inside["clients"][0]["in_use"] is True
    assert inside["summary"]["any_client_open"] is True
    assert outside["clients"][0]["seconds_since_seen"] == 46.0
    assert outside["clients"][0]["in_use"] is False
    assert outside["summary"]["any_client_open"] is False


def test_a_hidden_page_is_open_but_not_in_use() -> None:
    with _client() as client:
        _heartbeat(client, page_visible=False)
        payload = client.get("/api/v1/telemetry/clients").json()

    assert payload["clients"][0]["in_use"] is False
    assert payload["summary"]["any_client_open"] is True
    assert payload["summary"]["any_client_in_use"] is False
    assert payload["summary"]["desktop_shell_open"] is True


def test_clients_seen_since_boot_are_still_listed_when_stale() -> None:
    clock = FakeClock()
    store = UsageStore(monotonic=clock)
    with _client(store) as client:
        _heartbeat(client)
        clock.advance(3_600.0)
        payload = client.get("/api/v1/telemetry/clients").json()

    assert len(payload["clients"]) == 1
    assert payload["clients"][0]["in_use"] is False
    assert payload["summary"] == {
        "any_client_open": False,
        "any_client_in_use": False,
        "desktop_shell_open": False,
    }


def test_summary_answers_the_question_without_a_human() -> None:
    with _client() as client:
        _heartbeat(client, client_id="tab-1", surface="browser", page_visible=True)
        payload = client.get("/api/v1/telemetry/clients").json()

    assert payload["summary"] == {
        "any_client_open": True,
        "any_client_in_use": True,
        "desktop_shell_open": False,
    }


def test_no_clients_since_boot_is_an_honest_empty_answer() -> None:
    with _client() as client:
        payload = client.get("/api/v1/telemetry/clients").json()

    assert payload["clients"] == []
    assert payload["summary"]["any_client_open"] is False
    assert payload["passive_activity"]["last_request_at"] is None
    assert payload["in_use_window_seconds"] == IN_USE_WINDOW_SECONDS


# ----------------------------------------------------------- passive signal


def test_a_shell_request_to_a_normal_path_is_passive_app_usage() -> None:
    with _client() as client:
        client.get("/api/v1/tracks", headers={"user-agent": SHELL_UA})
        passive = client.get("/api/v1/telemetry/clients").json()["passive_activity"]

    assert passive["last_request_at"] is not None
    assert passive["by_surface"]["desktop-shell"]["last_request_at"] is not None
    assert passive["by_surface"]["browser"]["last_request_at"] is None


def test_a_browser_request_is_attributed_to_the_browser_surface() -> None:
    with _client() as client:
        client.get("/api/v1/tracks", headers={"user-agent": CHROME_UA})
        passive = client.get("/api/v1/telemetry/clients").json()["passive_activity"]

    assert passive["by_surface"]["browser"]["last_request_at"] is not None
    assert passive["by_surface"]["desktop-shell"]["last_request_at"] is None


def test_telemetry_endpoints_do_not_count_as_app_usage() -> None:
    with _client() as client:
        _heartbeat(client)
        client.get("/api/v1/telemetry/clients", headers={"user-agent": SHELL_UA})
        passive = client.get(
            "/api/v1/telemetry/clients", headers={"user-agent": SHELL_UA}
        ).json()["passive_activity"]

    assert passive["last_request_at"] is None


def test_health_polling_does_not_count_as_app_usage() -> None:
    with _client() as client:
        client.get("/api/v1/health", headers={"user-agent": SHELL_UA})
        passive = client.get("/api/v1/telemetry/clients").json()["passive_activity"]

    assert passive["last_request_at"] is None


def test_curl_and_agent_traffic_is_not_app_usage() -> None:
    with _client() as client:
        client.get("/api/v1/tracks", headers={"user-agent": CURL_UA})
        client.get("/api/v1/tracks", headers={"user-agent": ""})
        passive = client.get("/api/v1/telemetry/clients").json()["passive_activity"]

    assert passive["last_request_at"] is None


def test_passive_seconds_since_last_request_uses_the_injected_clock() -> None:
    clock = FakeClock()
    store = UsageStore(monotonic=clock)
    with _client(store) as client:
        client.get("/api/v1/tracks", headers={"user-agent": SHELL_UA})
        clock.advance(12.0)
        passive = client.get("/api/v1/telemetry/clients").json()["passive_activity"]

    assert passive["seconds_since_last_request"] == 12.0
    assert passive["by_surface"]["desktop-shell"]["seconds_since_request"] == 12.0

pytestmark = pytest.mark.rb_parity
