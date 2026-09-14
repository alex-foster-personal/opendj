"""Share-host gate: Access/token auth, read-only, local stays full access."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.share_gate import (
    AUTH_CLOUDFLARE_ACCESS,
    AUTH_TOKEN,
    ShareConfig,
)

SHARE_HOST = "dj.example"
TOKEN = "share-secret-token"


def _client(config: ShareConfig) -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        share_config=config,
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def share_client() -> TestClient:
    return _client(
        ShareConfig(
            host=SHARE_HOST,
            auth=AUTH_TOKEN,
            token=TOKEN,
            read_only=True,
        )
    )


@pytest.fixture
def access_client() -> TestClient:
    return _client(
        ShareConfig(
            host=SHARE_HOST,
            auth=AUTH_CLOUDFLARE_ACCESS,
            read_only=True,
        )
    )


ACCESS_HEADERS = {
    "Host": SHARE_HOST,
    "Cf-Access-Authenticated-User-Email": "tester@example.test",
    # cloudflared verifies the signature/audience before this reaches FastAPI.
    "Cf-Access-Jwt-Assertion": "present-and-validated-by-cloudflared",
}


def test_loopback_stays_open_without_token(share_client: TestClient) -> None:
    resp = share_client.get("/api/v1/health")
    assert resp.status_code == 200


def test_share_host_without_token_is_401(share_client: TestClient) -> None:
    resp = share_client.get(
        "/api/v1/settings",
        headers={"Host": SHARE_HOST},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "SHARE_UNAUTHORIZED"


def test_share_host_with_token_can_read(share_client: TestClient) -> None:
    resp = share_client.get(
        "/api/v1/settings",
        headers={
            "Host": SHARE_HOST,
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    assert resp.status_code == 200


def test_share_host_is_read_only(share_client: TestClient) -> None:
    resp = share_client.patch(
        f"/api/v1/tracks/{'a' * 40}",
        headers={
            "Host": SHARE_HOST,
            "Authorization": f"Bearer {TOKEN}",
        },
        json={"rating": 5},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "SHARE_READ_ONLY"


def test_health_is_exempt_on_share_host(share_client: TestClient) -> None:
    resp = share_client.get("/api/v1/health", headers={"Host": SHARE_HOST})
    assert resp.status_code == 200


@pytest.mark.requirement("OPS-32")
def test_health_on_share_host_without_token_discloses_no_benign_env_name(
    share_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if an unauthenticated share-host caller ever sees a non-forbidden env
    var NAME in /api/v1/health then broken.

    OPS-32 round 4 (issue #2637/#2638 follow-on, Mon 14 Sep 2026): this is
    the exact path the round exists to close. `/api/v1/health` is exempt
    from this module's own auth gate in token mode (see
    `test_health_is_exempt_on_share_host` above, and
    `share_gate.EXEMPT_SUFFIXES` / `share_gate_middleware`'s
    `if config.auth == AUTH_TOKEN and is_exempt(...)` early return) so that
    cloudflared can probe liveness before a token is presented. Round 3's
    `process_env_keys` field handed that same unauthenticated caller the
    full sorted list of every configured env var name -- which services and
    secrets this install has configured, on a repo going public. Round 4
    narrows the field to `process_env_forbidden_keys` (names already
    matching a small forbidden-prefix constant) plus a control bit, so a
    benign, non-forbidden-prefix name a real install would also have set
    must never appear in the body an unauthenticated share-host caller
    receives.

    - [if] a token-less share-host caller sees a benign env name [then] this fails, [else stop].
    """
    monkeypatch.setenv("OPS32_BENIGN_CANARY", "irrelevant-value")
    resp = share_client.get("/api/v1/health", headers={"Host": SHARE_HOST})
    assert resp.status_code == 200
    assert "OPS32_BENIGN_CANARY" not in resp.text
    body = resp.json()
    assert "OPS32_BENIGN_CANARY" not in body["process_env_forbidden_keys"]


def test_access_mode_rejects_missing_identity_even_for_health(
    access_client: TestClient,
) -> None:
    resp = access_client.get("/api/v1/health", headers={"Host": SHARE_HOST})
    assert resp.status_code == 401
    assert resp.json()["code"] == "SHARE_UNAUTHORIZED"


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": SHARE_HOST, "Cf-Access-Authenticated-User-Email": "x@y.test"},
        {"Host": SHARE_HOST, "Cf-Access-Jwt-Assertion": "assertion"},
    ],
)
def test_access_mode_requires_both_forwarded_identity_headers(
    access_client: TestClient,
    headers: dict[str, str],
) -> None:
    resp = access_client.get("/api/v1/settings", headers=headers)
    assert resp.status_code == 401


def test_access_mode_can_read_after_cloudflared_validation(
    access_client: TestClient,
) -> None:
    resp = access_client.get("/api/v1/settings", headers=ACCESS_HEADERS)
    assert resp.status_code == 200


def test_access_mode_remains_read_only(access_client: TestClient) -> None:
    resp = access_client.patch(
        f"/api/v1/tracks/{'a' * 40}",
        headers=ACCESS_HEADERS,
        json={"rating": 5},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "SHARE_READ_ONLY"


def test_access_mode_does_not_gate_tailnet_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSIC_DJ_ALLOWED_HOSTS", "agentbox.example-tailnet.ts.net")
    client = _client(
        ShareConfig(
            host=SHARE_HOST,
            auth=AUTH_CLOUDFLARE_ACCESS,
            read_only=True,
        )
    )
    resp = client.get(
        "/api/v1/settings",
        headers={"Host": "agentbox.example-tailnet.ts.net"},
    )
    assert resp.status_code == 200


def test_access_mode_disables_legacy_token_session(access_client: TestClient) -> None:
    resp = access_client.get(
        "/api/v1/share/session?share=anything",
        headers=ACCESS_HEADERS,
    )
    assert resp.status_code == 404


def test_share_config_defaults_to_access_without_token() -> None:
    config = ShareConfig.from_environ({"MUSIC_DJ_SHARE_HOST": SHARE_HOST})
    assert config.auth == AUTH_CLOUDFLARE_ACCESS


def test_explicit_token_mode_fails_closed_without_token() -> None:
    with pytest.raises(ValueError, match="MUSIC_DJ_SHARE_TOKEN is required"):
        ShareConfig.from_environ(
            {
                "MUSIC_DJ_SHARE_HOST": SHARE_HOST,
                "MUSIC_DJ_SHARE_AUTH": AUTH_TOKEN,
            }
        )

pytestmark = pytest.mark.rb_parity
