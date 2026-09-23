"""Remote transport bind and gate behavior (AGENT-17)."""

from __future__ import annotations

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from apps.fleet_mcp.access import ACCESS_JWT_HEADER, AccessRefusal
from apps.fleet_mcp.remote import (
    DEFAULT_HOST,
    HEALTH_PATH,
    BindRefused,
    access_middleware,
    build_app,
    require_loopback,
)


class _AlwaysRefuses:
    def verify(self, headers):
        raise AccessRefusal("access_jwt_missing", "no token")


class _AlwaysAllows:
    def __init__(self):
        self.seen = []

    def verify(self, headers):
        self.seen.append(dict(headers))
        return "dev@example.com"


@pytest.mark.requirement("AGENT-17")
def test_the_default_bind_is_loopback():
    """[if] no host is given [then] the endpoint binds loopback, [else stop]"""
    assert require_loopback(DEFAULT_HOST) == DEFAULT_HOST


@pytest.mark.requirement("AGENT-17")
@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::", "agentbox.example"])
def test_a_non_loopback_bind_is_refused(host):
    """[if] the bind would be reachable off-box [then] refuse to serve, [else stop]

    0.0.0.0 is the specific mistake: on a box with a public IP it publishes a
    write-capable MCP endpoint with no door in front of it, and no correct
    Cloudflare configuration can take that back.
    """
    with pytest.raises(BindRefused):
        require_loopback(host)


@pytest.mark.requirement("AGENT-17")
def test_ipv6_loopback_is_allowed():
    """[if] the bind is ::1 [then] it is loopback and allowed, [else stop]"""
    assert require_loopback("::1") == "::1"


@pytest.mark.requirement("AGENT-17")
def test_an_unauthenticated_request_is_refused_with_a_machine_readable_code():
    """[if] a request carries no Access token [then] it is refused 403, [else stop]"""
    client = TestClient(build_app(verifier=_AlwaysRefuses()))
    response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert response.status_code == 403
    assert response.json()["error"] == "access_jwt_missing"


@pytest.mark.requirement("AGENT-17")
def test_health_answers_without_a_token_and_reads_no_fleet_state():
    """[if] the unit probes health [then] it answers without Access, [else stop]

    Scoped deliberately: liveness only. A health path that reported queue or
    nucbox state would be an unauthenticated read of the thing this endpoint
    exists to protect.
    """
    client = TestClient(build_app(verifier=_AlwaysRefuses()))
    response = client.get(HEALTH_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.requirement("AGENT-17")
def test_every_other_path_is_gated_not_just_the_mcp_one():
    """[if] any non-health path is requested [then] it goes through the gate, [else stop]

    A gate that only covered one known path would be satisfied by any route
    the transport adds later.
    """
    client = TestClient(build_app(verifier=_AlwaysRefuses()))
    for path in ("/", "/mcp", "/messages", "/sse", "/anything/else"):
        assert client.get(path).status_code == 403, path


@pytest.mark.requirement("AGENT-17")
def test_the_verified_email_reaches_the_app_for_attribution():
    """[if] a request is verified [then] the signed email is put on the scope, [else stop]

    Driven against a stub downstream rather than the real MCP app: the point
    under test is the middleware's contract, and the transport needs a running
    session manager that would only add a second reason for this to fail.
    """
    seen: dict[str, object] = {}

    async def downstream(scope, receive, send):
        seen.update(scope.get("state", {}))
        await JSONResponse({"ok": True})(scope, receive, send)

    verifier = _AlwaysAllows()
    client = TestClient(access_middleware(downstream, verifier))
    response = client.get("/mcp", headers={ACCESS_JWT_HEADER: "token"})

    assert response.status_code == 200
    assert seen["access_email"] == "dev@example.com"


@pytest.mark.requirement("AGENT-17")
def test_a_refused_request_never_reaches_the_app():
    """[if] the gate refuses [then] the downstream app is never called, [else stop]

    The load-bearing direction: a gate that refused AFTER invoking the tool
    would pass every assertion about status codes and still have run the tool.
    """
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        await JSONResponse({"ok": True})(scope, receive, send)

    client = TestClient(access_middleware(downstream, _AlwaysRefuses()))
    assert client.get("/mcp").status_code == 403
    assert not called
