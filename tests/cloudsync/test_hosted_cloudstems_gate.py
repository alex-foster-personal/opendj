"""CloudStems paid gate on a hosted hub (CSPAY-05).

Every hub here is the real sync router on a real migrated sqlite DB. The stem
index is injected through ``app.state.stem_index_fetcher`` so no R2 call runs.

- [if] an archived owner can read the hosted stem index [then] broken, [else stop].
- [if] a self-hosted hub asks a source about CloudStems [then] broken, [else stop].
- [if] Polar is down and stems still serve [then] broken, [else stop].
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.entitlements import Standing, StaticEntitlementSource
from apps.entitlements import polar as polar_module
from apps.entitlements.lifecycle import LifecycleState
from apps.entitlements.polar import PolarClient, PolarConfig, PolarEntitlementSource
from apps.shared.state import machine_identity
from apps.sync_hub import entitlement_gate
from apps.sync_hub import service as sync_service

from .conftest import ENROLL_OWNER_SUB
from .enrollment_helpers import http_enroll, mint_grant
from .enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("CSPAY-05")

FEATURE: str = entitlement_gate.CLOUDSTEMS_HOSTED_FEATURE
INDEX_PATH: str = "/api/v1/sync/stems/index"
_MINIMAL_INDEX: dict[str, dict[str, str]] = {
    "track-a": {"manifest.json": "a" * 64},
}


@pytest.fixture(autouse=True)
def _observe_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")


class CloudstemsSource:
    """Static standings for ``cloudstems.hosted``, with a consult log."""

    def __init__(self, state: LifecycleState | None) -> None:
        table = {} if state is None else {(ENROLL_OWNER_SUB, FEATURE): Standing(state, None)}
        self._inner = StaticEntitlementSource(table, provider="cloudstems-static")
        self.asked: list[tuple[str, str]] = []

    @property
    def provider(self) -> str:
        return self._inner.provider

    def standing(self, subject: str, feature_id: str) -> Standing | None:
        self.asked.append((subject, feature_id))
        return self._inner.standing(subject, feature_id)


def _outage_polar_source(tmp_path: Path) -> PolarEntitlementSource:
    """The real Polar source and client on the real httpx network transport."""
    config = PolarConfig(
        access_token="polar_oat_test",
        server="sandbox",
        product_features={"cloudstems-product": FEATURE},
        read_only_days=90,
        success_url="https://open-dj.com/thanks?checkout_id={CHECKOUT_ID}",
        snapshot_path=tmp_path / "polar-snap.sqlite",
    )
    return PolarEntitlementSource(config, fetch=PolarClient(config).subscriptions)


@contextmanager
def _polar_answers_503() -> Iterator[tuple[str, list[str]]]:
    """A real local HTTP server standing where Polar is, answering every request 503."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            body = b"service unavailable"
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@contextmanager
def _polar_refuses_connection() -> Iterator[tuple[str, list[str]]]:
    """A local port with nothing listening, so the connect itself is refused."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    yield f"http://127.0.0.1:{port}", []


def _hub_app(hub_dir: Path, *, hosted: bool | None) -> FastAPI:
    from apps.sync_hub import client as sync_client

    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "cloudstems-hub"
    if hosted is not None:
        app.state.sync_hub_hosted = hosted
    app.state.stem_index_fetcher = lambda: _MINIMAL_INDEX
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


@pytest.fixture
def hosted_stems_hub(enroll_hub_dir: Path) -> Iterator[tuple[TestClient, str, CloudstemsSource]]:
    source = CloudstemsSource("active")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke"
        spoke_dir.mkdir()
        http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        yield http, machine_id, source


def _get_index(http: TestClient, machine_id: str) -> Any:
    return http.get(INDEX_PATH, params={"machine_id": machine_id})


def test_active_owner_gets_the_stem_index(hosted_stems_hub: Any) -> None:
    http, machine_id, source = hosted_stems_hub
    response = _get_index(http, machine_id)
    assert response.status_code == 200, response.text
    assert response.json()["index"] == _MINIMAL_INDEX
    assert source.asked == [(ENROLL_OWNER_SUB, FEATURE)]


def test_archived_owner_is_refused_the_stem_index(enroll_hub_dir: Path) -> None:
    source = CloudstemsSource("archived")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-archived"
        spoke_dir.mkdir()
        http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = _get_index(http, machine_id)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "entitlement_not_in_plan"


def test_self_hosted_hub_never_consults_the_source_for_stems(enroll_hub_dir: Path) -> None:
    source = CloudstemsSource("archived")
    app = _hub_app(enroll_hub_dir, hosted=None)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-self"
        spoke_dir.mkdir()
        http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = _get_index(http, machine_id)
    assert response.status_code == 200, response.text
    assert source.asked == []


@pytest.mark.parametrize("polar", [_polar_answers_503, _polar_refuses_connection], ids=["http-503", "connect-refused"])
def test_polar_outage_refuses_stems_with_provider_code(
    enroll_hub_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    polar: Callable[[], AbstractContextManager[tuple[str, list[str]]]],
) -> None:
    """The real Polar source and client in enforce mode, over a real socket that fails."""
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "enforce")
    with polar() as (base_url, seen):
        # The one test seam: the sandbox server name resolves to the failing local endpoint.
        monkeypatch.setitem(polar_module.BASE_URLS, "sandbox", base_url)
        app = _hub_app(enroll_hub_dir, hosted=True)
        app.state.entitlement_source = _outage_polar_source(tmp_path)
        with TestClient(app) as http:
            spoke_dir = enroll_hub_dir.parent / "spoke"
            spoke_dir.mkdir()
            enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
            credential = enrolled["sync_credential"]
            assert isinstance(credential, str) and credential
            machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
            response = http.get(
                INDEX_PATH, params={"machine_id": machine_id}, headers={"Authorization": f"Bearer {credential}"}
            )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == entitlement_gate.PROVIDER_CODE
    if polar is _polar_answers_503:
        assert seen, "the request never reached the Polar endpoint, so no outage was exercised"
        assert all("/subscriptions/" in path for path in seen), seen


def test_gated_routes_document_every_503_they_can_return() -> None:
    """if composing auth and gate responses drops SYNC_ENFORCE_NOT_ACTIVE from a 503 then broken."""
    from apps.sync_hub import entitlement_gate as gate

    app = FastAPI()
    app.include_router(sync_service.router, prefix="/api/v1")
    paths = app.openapi()["paths"]
    for path, method in (
        ("/api/v1/sync/stems/index", "get"),
        ("/api/v1/sync/stems/bundle-presign", "get"),
        ("/api/v1/sync/pull", "get"),
        ("/api/v1/sync/push", "post"),
    ):
        text = paths[path][method]["responses"]["503"]["description"]
        assert "SYNC_ENFORCE_NOT_ACTIVE" in text, (path, text)
        assert gate.NO_SOURCE_CODE in text, (path, text)


def test_merge_responses_refuses_two_models_for_one_status() -> None:
    """if two different body models for one status are merged silently then broken."""
    from pydantic import BaseModel

    from apps.sync_hub.entitlement_gate import merge_responses

    class A(BaseModel):
        a: int

    class B(BaseModel):
        b: int

    with pytest.raises(ValueError):
        merge_responses({503: {"model": A, "description": "a"}}, {503: {"model": B, "description": "b"}})


def test_merge_responses_keeps_every_field_from_a_colliding_entry() -> None:
    merge_responses = entitlement_gate.merge_responses
    merged = merge_responses(
        {503: {"description": "a", "headers": {"Retry-After": {"schema": {"type": "integer"}}}}},
        {503: {"description": "b", "content": {"application/json": {}}, "links": {"x": {}}}},
    )
    assert merged == {
        503: {
            "description": "a Or: b",
            "headers": {"Retry-After": {"schema": {"type": "integer"}}},
            "content": {"application/json": {}},
            "links": {"x": {}},
        }
    }
    # Control: the same field with two different values is refused, not overwritten.
    with pytest.raises(ValueError, match="'headers'"):
        merge_responses(
            {503: {"description": "a", "headers": {"A": {}}}}, {503: {"description": "b", "headers": {"B": {}}}}
        )
    # A numeric status written as a string is the same OpenAPI status.
    assert merge_responses({503: {"description": "a"}}, {"503": {"description": "b"}}) == {
        503: {"description": "a Or: b"}
    }
    with pytest.raises(ValueError, match="'headers'"):
        merge_responses(
            {503: {"description": "a", "headers": {"A": {}}}}, {"503": {"description": "b", "headers": {"B": {}}}}
        )
    # Control: an identical field on both sides is not a conflict.
    assert merge_responses({409: {"description": "a", "links": {}}}, {409: {"description": "b", "links": {}}}) == {
        409: {"description": "a Or: b", "links": {}}
    }
