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
from datetime import UTC, datetime
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
from apps.sync_hub import entitlement_gate, stem_meter
from apps.sync_hub import service as sync_service

from .conftest import ENROLL_OWNER_SUB
from .enrollment_helpers import http_enroll, mint_grant
from .enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("CSPAY-05")

FEATURE: str = entitlement_gate.CLOUDSTEMS_HOSTED_FEATURE
INDEX_PATH: str = "/api/v1/sync/stems/index"
PRESIGN_PATH: str = "/api/v1/sync/stems/bundle-presign"
_MINIMAL_INDEX: dict[str, dict[str, str]] = {
    "track-a": {"manifest.json": "a" * 64},
}


@pytest.fixture(autouse=True)
def _observe_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")
    # CSPAY-07: a hosted hub meters CloudStems and the cap has no default.
    monkeypatch.setenv("MDT_CLOUDSTEMS_MONTHLY_CAP", "200")


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


def _seed_usage(hub_dir: Path, *, stable_id: str, cap: int) -> None:
    """Count one bundle for the enrolled owner in the CURRENT UTC month."""
    import sqlite3

    from apps.sync_hub import client as sync_client
    from apps.sync_hub import stem_meter

    conn = sqlite3.connect(sync_client.state_db_path(hub_dir))
    try:
        now = datetime.now(UTC)
        stem_meter.reserve(
            conn,
            owner_sub=ENROLL_OWNER_SUB,
            month=stem_meter.month_of(now),
            stable_id=stable_id,
            cap=cap,
            counted_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        conn.commit()
    finally:
        conn.close()


def test_presign_refuses_when_the_monthly_cap_is_unconfigured(
    hosted_stems_hub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a hosted hub meters CloudStems on a guessed cap then broken."""
    http, machine_id, _source = hosted_stems_hub
    monkeypatch.delenv("MDT_CLOUDSTEMS_MONTHLY_CAP", raising=False)
    response = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == stem_meter.CAP_UNSET_CODE


def test_presign_refuses_a_bundle_past_the_monthly_cap(
    hosted_stems_hub: Any, enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if the hub serves bundle N+1 after the owner used N of N then broken."""
    http, machine_id, _source = hosted_stems_hub
    monkeypatch.setenv("MDT_CLOUDSTEMS_MONTHLY_CAP", "1")
    _seed_usage(enroll_hub_dir, stable_id="track-a", cap=1)
    response = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-b"}
    )
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["code"] == stem_meter.CAP_EXCEEDED_CODE


def _used_this_month(hub_dir: Path) -> int:
    """Tracks the hub has charged this owner in the current UTC month."""
    import sqlite3

    from apps.sync_hub import client as sync_client

    conn = sqlite3.connect(sync_client.state_db_path(hub_dir))
    try:
        return stem_meter.used(
            conn,
            owner_sub=ENROLL_OWNER_SUB,
            month=stem_meter.month_of(datetime.now(UTC)),
        )
    finally:
        conn.close()


def _stub_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the hub's R2 clients, HEAD and presign with local stubs.

    The presign path is the only place the hub talks to R2, and every test
    below is about the METER around it, so the transport is stubbed and the
    metering code under test is the real one.
    """
    from types import SimpleNamespace

    from apps.sync_hub import service_stem_assets

    monkeypatch.setattr(
        service_stem_assets, "_hub_r2_clients", lambda: (object(), object())
    )
    monkeypatch.setattr(
        service_stem_assets.stem_index,
        "fetch_index",
        lambda _cfg, _s3: _MINIMAL_INDEX,
    )
    monkeypatch.setattr(
        service_stem_assets.asset_store,
        "head_asset",
        lambda _cfg, _s3, _digest: SimpleNamespace(size=7),
    )
    monkeypatch.setattr(
        service_stem_assets.asset_store,
        "presign_url",
        lambda _cfg, digest, _expiry: f"https://r2.example/{digest}",
    )


def test_an_unindexed_bundle_does_not_use_up_a_track(
    hosted_stems_hub: Any, enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a stable_id the hub cannot serve costs the owner a track [then] broken.

    CORE review P1 on #7322: the meter must charge only bundles the hub
    actually presigned, so an unindexed id (404) or an R2 failure (502) has
    to leave ``used()`` untouched.
    """
    http, machine_id, _source = hosted_stems_hub
    _stub_r2(monkeypatch)
    monkeypatch.setenv("MDT_CLOUDSTEMS_MONTHLY_CAP", "1")
    response = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-missing"}
    )
    assert response.status_code == 404, response.text
    assert _used_this_month(enroll_hub_dir) == 0


def test_an_r2_failure_does_not_use_up_a_track(
    hosted_stems_hub: Any, enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a missing R2 object costs the owner a track [then] broken."""
    from apps.sync_hub import service_stem_assets

    http, machine_id, _source = hosted_stems_hub
    _stub_r2(monkeypatch)
    monkeypatch.setattr(
        service_stem_assets.asset_store, "head_asset", lambda _cfg, _s3, _digest: None
    )
    monkeypatch.setenv("MDT_CLOUDSTEMS_MONTHLY_CAP", "1")
    response = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert response.status_code == 502, response.text
    assert _used_this_month(enroll_hub_dir) == 0


def test_a_presigned_bundle_is_charged_exactly_once(
    hosted_stems_hub: Any, enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a served bundle is not counted (or a retry is) [then] broken."""
    http, machine_id, _source = hosted_stems_hub
    _stub_r2(monkeypatch)
    # A probe before the fetch costs nothing; only the fetch itself charges.
    probe = http.get(
        SIZE_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert probe.status_code == 200, probe.text
    assert _used_this_month(enroll_hub_dir) == 0
    first = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["files"]) == 1
    assert _used_this_month(enroll_hub_dir) == 1
    again = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert again.status_code == 200, again.text
    assert _used_this_month(enroll_hub_dir) == 1


SIZE_PATH: str = "/api/v1/sync/stems/bundle-size"


def test_a_size_probe_spends_no_track_and_mints_no_url(
    hosted_stems_hub: Any, enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a size probe charges a track or returns a url [then] broken.

    Codex round 3, P1: ``bundle_remote_size`` runs BEFORE the byte budget is
    applied, so charging the presign it used to call billed bundles that were
    never downloaded. The probe answers sizes only, at a spent month, and the
    presign helper is never reached.
    """
    from apps.sync_hub import service_stem_assets

    http, machine_id, _source = hosted_stems_hub
    _stub_r2(monkeypatch)

    def _never_presign(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("the size probe must not presign anything")

    monkeypatch.setattr(
        service_stem_assets.asset_store, "presign_url", _never_presign
    )
    monkeypatch.setenv("MDT_CLOUDSTEMS_MONTHLY_CAP", "1")
    # The month is already spent: the probe is unmetered, so it still answers.
    _seed_usage(enroll_hub_dir, stable_id="track-a", cap=1)
    response = http.get(
        SIZE_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stable_id"] == "track-a"
    assert body["files"] == [
        {"filename": "manifest.json", "content_hash": "a" * 64, "size_bytes": 7}
    ]
    assert "url" not in response.text
    assert _used_this_month(enroll_hub_dir) == 1


def test_bulk_hydrate_over_budget_probes_size_and_charges_nothing(
    hosted_stems_hub: Any, enroll_hub_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION CONTROL: a bundle too big for the budget must cost 0 tracks.

    With the charged presign as the probe, this same call billed the owner for
    a bundle ``bulk_hydrate`` then skipped. The spoke now measures through
    ``/stems/bundle-size``, so the skip is free.
    """
    from apps.cloud.stem_hydration import bulk_hydrate
    from apps.cloud.stem_source import HubPresignedSource

    http, machine_id, _source = hosted_stems_hub
    _stub_r2(monkeypatch)
    spoke_dir = enroll_hub_dir.parent / "spoke"
    source = HubPresignedSource(
        data_dir=spoke_dir,
        hub_url="http://hub.invalid",
        machine_id=machine_id,
        bearer=None,
        transport=TestClientTransport(http),
    )
    report = bulk_hydrate(
        ["track-a"],
        data_dir=spoke_dir,
        source=source,
        index=_MINIMAL_INDEX,
        byte_budget=1,  # the bundle is 7 bytes, so it cannot fit
        stems_dir=tmp_path / "stems",
    )
    assert report.fetched == ()
    assert len(report.skipped) == 1
    assert report.skipped[0].status == "unavailable"
    assert "byte_budget exhausted" in (report.skipped[0].reason or "")
    assert _used_this_month(enroll_hub_dir) == 0


def test_the_cap_refusals_declare_a_body_in_the_contract() -> None:
    """[if] the cap refusals carry no body in the contract [then] broken.

    Codex round 8: a description-only response entry regenerates into
    api-types.ts as ``content?: never``, so a typed client can see the 429 but
    cannot read ``detail.code`` at all. Both hub refusals and the spoke's 429
    now declare GateErrorResponse.
    """
    import json

    root = Path(__file__).resolve().parents[2]
    doc = json.loads((root / "apps" / "webui" / "openapi.json").read_text(encoding="utf-8"))
    presign = doc["paths"]["/api/v1/sync/stems/bundle-presign"]["get"]["responses"]
    for status in ("429", "503"):
        assert "content" in presign[status], f"hub bundle-presign {status} declares no body"
        schema = presign[status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/GateErrorResponse"), schema
    tracks = doc["paths"]["/api/v1/tracks/{stable_id}/stems"]["get"]["responses"]
    assert "content" in tracks["429"], "spoke manifest 429 declares no body"
    # Codex round 9 / Sol on 91035ad: the spoke's 503s (UNSET, unreachable,
    # still hydrating) are structured bodies too, including the part route
    # whose 503 entry is merged over the base one, and the bulk route.
    spoke = {
        "/api/v1/tracks/{stable_id}/stems": "get",
        "/api/v1/tracks/{stable_id}/stems/{part}": "get",
        "/api/v1/stems/bulk-hydrate": "post",
    }
    for path, method in spoke.items():
        responses = doc["paths"][path][method]["responses"]
        assert "content" in responses["503"], f"spoke {path} 503 declares no body"
        schema = responses["503"]["content"]["application/json"]["schema"]
        # Sol on 27f9acf396: an unreachable hub and a relayed cap refusal are
        # different bodies, so the 503 names both models, not one.
        refs = sorted(option["$ref"].rsplit("/", 1)[-1] for option in schema["anyOf"])
        assert refs == ["GateErrorResponse", "HubErrorResponse"], (path, schema)
    # Sol on 61dd5ee997: the hub's own 404/502 for bundle-presign and
    # bundle-size send structured detail.code bodies as well.
    for path in ("/api/v1/sync/stems/bundle-presign", "/api/v1/sync/stems/bundle-size"):
        responses = doc["paths"][path]["get"]["responses"]
        # Codex on a3b753655e: an unsafe stable_id is a structured 400 too.
        for status in ("400", "404", "502"):
            assert "content" in responses[status], f"hub {path} {status} declares no body"
            schema = responses[status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/GateErrorResponse"), (path, status, schema)


def test_the_meter_bills_the_owner_whose_entitlement_passed(
    hosted_stems_hub: Any, enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] billing re-reads enrollment after the gate [then] broken.

    Sol on 61dd5ee997 and f9e7f65676 (P1): a second enrollment read after
    ``require`` can see nobody (served unmetered) or another owner (charged to
    someone whose entitlement was never checked). ``require`` now returns the
    subject it authorized and the meter bills that. Here every enrollment read
    after the first names another owner, so a second read would bill them.
    """
    import dataclasses
    import sqlite3

    from apps.sync_hub import client as sync_client

    http, machine_id, _source = hosted_stems_hub
    _stub_r2(monkeypatch)
    real_owner_for = entitlement_gate.enrollment.owner_for
    reads: list[str | None] = []

    def _reassigned_after_first_read(*args: Any, **kwargs: Any) -> Any:
        owner = real_owner_for(*args, **kwargs)
        if reads and owner is not None:
            owner = dataclasses.replace(owner, google_sub="owner-b")
        reads.append(None if owner is None else owner.google_sub)
        return owner

    monkeypatch.setattr(
        entitlement_gate.enrollment, "owner_for", _reassigned_after_first_read
    )
    response = http.get(
        PRESIGN_PATH, params={"machine_id": machine_id, "stable_id": "track-a"}
    )
    assert response.status_code == 200, response.text
    assert reads[0] == ENROLL_OWNER_SUB
    conn = sqlite3.connect(sync_client.state_db_path(enroll_hub_dir))
    try:
        billed = conn.execute(
            f"SELECT DISTINCT owner_sub FROM {stem_meter.USAGE_TABLE}"
        ).fetchall()
    finally:
        conn.close()
    assert billed == [(ENROLL_OWNER_SUB,)], billed
