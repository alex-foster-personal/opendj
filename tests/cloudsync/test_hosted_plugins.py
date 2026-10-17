"""Plugin catalog paid gate on a hosted hub (CSPAY-06).

Every hub here is the real sync router on a real migrated sqlite DB.

- [if] an archived owner can read the plugin catalog [then] broken, [else stop].
- [if] a read_only owner can read the plugin catalog [then] broken, [else stop].
- [if] a self-hosted hub asks a source about pluginstore.access [then] broken, [else stop].
- [if] an unset catalog env answers anything but 409 [then] broken, [else stop].
- [if] a malformed catalog file is served [then] broken, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.entitlements import Standing, StaticEntitlementSource
from apps.entitlements.lifecycle import LifecycleState
from apps.shared.state import machine_identity
from apps.sync_hub import client as hub_client
from apps.sync_hub import entitlement_gate
from apps.sync_hub import service as sync_service
from apps.sync_hub.service_plugins import CATALOG_ENV, CATALOG_INVALID_CODE, NOT_CONFIGURED_CODE, load_catalog

from .conftest import ENROLL_OWNER_SUB, seed_user
from .enrollment_helpers import http_enroll, mint_grant
from .enrollment_transport import TestClientTransport
from .test_hosted_entitlement_gate import SECOND_EMAIL, SECOND_SUB

pytestmark = pytest.mark.requirement("CSPAY-06")

FEATURE: str = entitlement_gate.PLUGINSTORE_ACCESS_FEATURE
INDEX_PATH: str = "/api/v1/sync/plugins/index"

_SAMPLE_PLUGIN = {
    "id": "demo.eq",
    "name": "Demo EQ",
    "version": "1.0.0",
    "url": "https://plugins.example.invalid/demo-eq-1.0.0.zip",
    "sha256": "a" * 64,
}


@pytest.fixture(autouse=True)
def _observe_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")


@pytest.fixture
def catalog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps([_SAMPLE_PLUGIN]), encoding="utf-8")
    monkeypatch.setenv(CATALOG_ENV, str(path))
    return path


class PluginSource:
    """Static standings for ``pluginstore.access``, with a consult log."""

    def __init__(self, state: LifecycleState | None) -> None:
        table = {} if state is None else {(ENROLL_OWNER_SUB, FEATURE): Standing(state, None)}
        self._inner = StaticEntitlementSource(table, provider="plugins-static")
        self.asked: list[tuple[str, str]] = []

    @property
    def provider(self) -> str:
        return self._inner.provider

    def standing(self, subject: str, feature_id: str) -> Standing | None:
        self.asked.append((subject, feature_id))
        return self._inner.standing(subject, feature_id)


def _hub_app(hub_dir: Path, *, hosted: bool | None) -> FastAPI:
    from apps.sync_hub import client as sync_client

    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "plugins-hub"
    if hosted is not None:
        app.state.sync_hub_hosted = hosted
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


@pytest.fixture
def hosted_plugins_hub(enroll_hub_dir: Path, catalog_file: Path) -> Iterator[tuple[TestClient, str, PluginSource]]:
    source = PluginSource("active")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        credential = enrolled["sync_credential"]
        assert isinstance(credential, str) and credential
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        http.headers.update({"Authorization": f"Bearer {credential}"})
        yield http, machine_id, source


def _get_index(http: TestClient, machine_id: str) -> Any:
    return http.get(INDEX_PATH, params={"machine_id": machine_id})


def test_active_owner_gets_the_plugin_catalog(hosted_plugins_hub: Any) -> None:
    http, machine_id, source = hosted_plugins_hub
    response = _get_index(http, machine_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plugins"] == [_SAMPLE_PLUGIN]
    assert source.asked == [(ENROLL_OWNER_SUB, FEATURE)]


def test_read_only_owner_is_refused_the_plugin_catalog(enroll_hub_dir: Path, catalog_file: Path) -> None:
    source = PluginSource("read_only")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-readonly"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        credential = enrolled["sync_credential"]
        assert isinstance(credential, str)
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {credential}"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "entitlement_not_in_plan"


def test_archived_owner_is_refused_the_plugin_catalog(enroll_hub_dir: Path, catalog_file: Path) -> None:
    source = PluginSource("archived")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-archived"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        credential = enrolled["sync_credential"]
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {credential}"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "entitlement_not_in_plan"


def test_unconfigured_catalog_env_is_409(enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CATALOG_ENV, raising=False)
    source = PluginSource("active")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-nocat"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {enrolled['sync_credential']}"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == NOT_CONFIGURED_CODE


def test_malformed_catalog_fails_loud(enroll_hub_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": "x"}]), encoding="utf-8")
    monkeypatch.setenv(CATALOG_ENV, str(bad))
    source = PluginSource("active")
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-badcat"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {enrolled['sync_credential']}"},
        )
    assert response.status_code == 502
    assert "validation" in response.json()["detail"]["message"].lower()


def test_self_hosted_hub_never_consults_the_source_for_plugins(enroll_hub_dir: Path, catalog_file: Path) -> None:
    source = PluginSource("archived")
    app = _hub_app(enroll_hub_dir, hosted=None)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-self"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        response = http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {enrolled['sync_credential']}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == [_SAMPLE_PLUGIN]
    assert source.asked == []


def _two_owner_index(enroll_hub_dir: Path, enroll_other_spoke_dir: Path, *, hosted: bool | None) -> Any:
    source = PluginSource("active")
    app = _hub_app(enroll_hub_dir, hosted=hosted)
    app.state.entitlement_source = source
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-two"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        seed_user(hub_client.state_db_path(enroll_hub_dir), sub=SECOND_SUB, email=SECOND_EMAIL)
        http_enroll(
            TestClientTransport(http),
            enroll_other_spoke_dir,
            name="second-owner-spoke",
            token=mint_grant(enroll_hub_dir, email=SECOND_EMAIL),
        )
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        return http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {enrolled['sync_credential']}"},
        )


def test_self_hosted_hub_with_two_owners_still_serves_plugins(
    enroll_hub_dir: Path, enroll_other_spoke_dir: Path, catalog_file: Path
) -> None:
    response = _two_owner_index(enroll_hub_dir, enroll_other_spoke_dir, hosted=None)
    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == [_SAMPLE_PLUGIN]


def test_hosted_hub_with_two_owners_refuses_plugins(
    enroll_hub_dir: Path, enroll_other_spoke_dir: Path, catalog_file: Path
) -> None:
    # Control: the hosted single-owner rule still applies to the catalog.
    response = _two_owner_index(enroll_hub_dir, enroll_other_spoke_dir, hosted=True)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == entitlement_gate.MULTI_OWNER_CODE


def test_non_utf8_catalog_is_a_catalog_error(tmp_path: Path) -> None:
    from fastapi import HTTPException

    path = tmp_path / "plugins.json"
    path.write_bytes(b"\xff\xfe[not utf8")
    with pytest.raises(HTTPException) as caught:
        load_catalog(path)
    assert caught.value.status_code == 502
    assert caught.value.detail["code"] == CATALOG_INVALID_CODE


def test_hosted_catalog_refuses_a_missing_credential_in_observe_mode(hosted_plugins_hub: Any) -> None:
    # Observe mode only logs a bad credential; the paid catalog must not.
    http, machine_id, source = hosted_plugins_hub
    del http.headers["Authorization"]
    response = _get_index(http, machine_id)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SYNC_CREDENTIAL"
    assert source.asked == []


def _hosted_index_with_catalog(enroll_hub_dir: Path, catalog: object, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = enroll_hub_dir.parent / "catalog-under-test.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setenv(CATALOG_ENV, str(path))
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = PluginSource("active")
    with TestClient(app) as http:
        spoke_dir = enroll_hub_dir.parent / "spoke-catalog"
        spoke_dir.mkdir()
        enrolled = http_enroll(TestClientTransport(http), spoke_dir, name="spoke", token=mint_grant(enroll_hub_dir))
        machine_id = machine_identity.get_or_create_machine_id(spoke_dir)
        return http.get(
            INDEX_PATH,
            params={"machine_id": machine_id},
            headers={"Authorization": f"Bearer {enrolled['sync_credential']}"},
        )


def test_catalog_entry_with_an_unknown_field_is_a_catalog_error(
    enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if an unknown catalog field is silently dropped and served then broken."""
    response = _hosted_index_with_catalog(enroll_hub_dir, [{**_SAMPLE_PLUGIN, "sha265": "typo"}], monkeypatch)
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == CATALOG_INVALID_CODE


@pytest.mark.parametrize("url", ["not a url", "http://plugins.example.invalid/x.zip", "https:///x.zip"])
def test_catalog_entry_with_a_bad_url_is_a_catalog_error(
    enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """if a non-https or malformed plugin url is served then broken."""
    response = _hosted_index_with_catalog(enroll_hub_dir, [{**_SAMPLE_PLUGIN, "url": url}], monkeypatch)
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == CATALOG_INVALID_CODE


def test_unset_catalog_is_not_revealed_to_an_unauthenticated_caller(
    hosted_plugins_hub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a caller with no credential learns the catalog env is unset then broken."""
    http, machine_id, source = hosted_plugins_hub
    monkeypatch.delenv(CATALOG_ENV, raising=False)
    del http.headers["Authorization"]
    response = _get_index(http, machine_id)
    assert response.status_code == 401, response.text
    assert source.asked == []


def test_cli_without_a_credential_reaches_a_self_hosted_observe_hub(
    enroll_live_hub: str, enroll_spoke_dir: Path, catalog_file: Path
) -> None:
    """if the CLI refuses a request the self-hosted observe hub would serve then broken."""
    from apps.sync_hub import config as sync_config
    from apps.sync_hub import spoke_credential
    from apps.sync_hub.plugins_cli import fetch_plugin_index

    sync_config.write_config(
        enroll_spoke_dir,
        sync_config.CloudSyncConfig(enabled=True, hub_url=enroll_live_hub, machine_name="spoke"),
    )
    assert spoke_credential.read_credential(enroll_spoke_dir) is None
    payload = fetch_plugin_index(enroll_spoke_dir)
    assert payload["plugins"] == [_SAMPLE_PLUGIN]
