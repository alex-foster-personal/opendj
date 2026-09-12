"""Per-machine sync credential and the OBSERVE/ENFORCE mode (plan X5).

Every assertion here drives the real sync router over the real ASGI stack
(``_enroll_hub_app``, the same builder the enrollment tests use) against a
real migrated sqlite hub, or a real uvicorn socket for the CLI test. Nothing
is mocked: a credential is minted by a real ``/enroll``, sent in a real
``Authorization`` header, and checked against the real ``machine_credentials``
row.

Regression one-liners:
  - if a spoofed machine_id without its credential reaches any endpoint under ENFORCE then broken
  - if the real credential is refused on any sync endpoint under ENFORCE then broken
  - if a revoked machine reaches any endpoint under ENFORCE then broken
  - if ENFORCE activates while a machine is unowned or uncredentialed then broken
  - if OBSERVE refuses a call main accepted, or hello misreports the verdict, then broken
  - if enroll returns the credential twice or the hub stores it unhashed then broken
  - if the spoke's credential file is not 0600, or sync does not send it, then broken

[if] a credential is minted [then] ENFORCE refuses bad ones, OBSERVE refuses none, [else stop].
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from apps.database.column_docs import COLUMN_DOCS
from apps.shared.state import schema as state_schema
from apps.shared.state.migrations_v11 import _V11
from apps.sync_hub import (
    capabilities,
    fleet_admin,
    machine_credentials,
    maintenance,
    protocol,
    spoke_credential,
)
from apps.sync_hub.machine_credentials import CREDENTIAL_PREFIX, MODE_ENV
from apps.sync_hub.transport import HttpTransport, SyncTransportError

from .conftest import ENROLL_OWNER_EMAIL, _enroll_hub_app
from .enrollment_helpers import http_enroll, machine_payload, mint_grant, read_hub
from .enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("CAT-04")

ENDPOINTS: tuple[str, ...] = ("hello", "push", "pull", "status", "digest")
SYNC: str = "/api/v1/sync"


@pytest.fixture
def hub_http(enroll_hub_dir: Path) -> Iterator[TestClient]:
    with TestClient(_enroll_hub_app(enroll_hub_dir)) as http:
        yield http


@pytest.fixture
def enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")


def _enroll(http: TestClient, hub_dir: Path, data_dir: Path, name: str) -> tuple[str, Any]:
    """Enroll through the real endpoint with a fresh grant; (machine_id, credential)."""
    body = http_enroll(TestClientTransport(http), data_dir, name=name, token=mint_grant(hub_dir))
    return str(body["machine_id"]), body["sync_credential"]


def _call(
    http: TestClient,
    endpoint: str,
    data_dir: Path,
    name: str,
    authorization: str | None = None,
) -> Response:
    """One call to ``endpoint`` AS the machine whose id lives in ``data_dir``."""
    headers = {} if authorization is None else {"Authorization": authorization}
    machine = machine_payload(data_dir, name=name)
    query = {"machine_id": machine["machine_id"], "capabilities": capabilities.QUARANTINE_V1}
    if endpoint == "hello":
        body = {
            "machine": machine,
            "schema_version": state_schema.SCHEMA_VERSION,
            "capabilities": list(capabilities.THIS_BUILD),
        }
        return http.post(f"{SYNC}/hello", json=body, headers=headers)
    if endpoint == "push":
        body = {
            "machine_id": machine["machine_id"],
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [],
            "capabilities": list(capabilities.THIS_BUILD),
        }
        return http.post(f"{SYNC}/push", json=body, headers=headers)
    if endpoint in ("pull", "status", "digest"):
        return http.get(f"{SYNC}/{endpoint}", params=query, headers=headers)
    raise AssertionError(f"unknown endpoint {endpoint!r}")


def _code(response: Response) -> str:
    return str(response.json()["detail"]["code"])


# ----- ENFORCE ---------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_enforce_refuses_a_spoofed_machine_id_on_every_endpoint(
    endpoint: str,
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    enforce: None,
) -> None:
    """if an enrolled machine_id without its credential gets anything but 401 then broken"""
    _a_id, credential_a = _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")
    _b_id, credential_b = _enroll(hub_http, enroll_hub_dir, enroll_other_spoke_dir, "spoke-b")
    spoofs = {
        "no Authorization header": None,
        "a forged bearer": f"Bearer {CREDENTIAL_PREFIX}forged",
        "another machine's REAL bearer": f"Bearer {credential_b}",
        "the right token under the wrong scheme": f"Basic {credential_a}",
    }
    for label, authorization in spoofs.items():
        response = _call(hub_http, endpoint, enroll_spoke_dir, "spoke-a", authorization)
        assert response.status_code == 401, f"{endpoint} with {label}: {response.text}"
        assert _code(response) == "SYNC_CREDENTIAL"
        assert response.headers["www-authenticate"] == "Bearer"

    accepted = _call(hub_http, endpoint, enroll_spoke_dir, "spoke-a", f"Bearer {credential_a}")
    assert accepted.status_code == 200, (
        f"control: the same {endpoint} call carrying spoke-a's own credential must "
        f"be accepted, or every refusal above is a refusal of everything: {accepted.text}"
    )


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_enforce_refuses_an_unknown_machine_id_the_same_way(
    endpoint: str,
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    enforce: None,
) -> None:
    """if ENFORCE answers an unknown id unlike a known one without its credential then broken"""
    _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")
    response = _call(hub_http, endpoint, enroll_other_spoke_dir, "never-enrolled")
    assert response.status_code == 401, response.text
    assert _code(response) == "SYNC_CREDENTIAL"


def test_a_revoked_machine_is_refused_everywhere(
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enforce: None,
) -> None:
    """if a revoked machine reaches any endpoint, or enroll lifts the revocation, then broken"""
    machine_id, credential = _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")
    bearer = f"Bearer {credential}"
    for endpoint in ENDPOINTS:
        before = _call(hub_http, endpoint, enroll_spoke_dir, "spoke-a", bearer)
        assert before.status_code == 200, f"control: {endpoint} before revoke: {before.text}"

    outcome = fleet_admin.revoke(enroll_hub_dir, machine_id=machine_id)
    assert outcome.changed and outcome.credential_deleted
    (owner,) = read_hub(enroll_hub_dir, "machine_owners")
    assert owner["revoked_at"] == outcome.revoked_at, "revoke must write revoked_at"
    assert read_hub(enroll_hub_dir, "machine_credentials") == []

    for endpoint in ENDPOINTS:
        after = _call(hub_http, endpoint, enroll_spoke_dir, "spoke-a", bearer)
        assert after.status_code == 401, f"{endpoint} after revoke: {after.text}"
        assert "revoked" in after.json()["detail"]["message"]
    with pytest.raises(SyncTransportError, match=r"HTTP 409.*SYNC_ENROLL_REVOKED"):
        _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")


def test_enforce_refuses_to_activate_while_a_machine_is_unowned(
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if ENFORCE serves anyone while an unowned or uncredentialed machine exists then broken"""
    _a_id, credential_a = _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")
    # spoke-b says hello under OBSERVE only: registered, unowned, no credential,
    # which is exactly what every machine that synced before v9 looks like.
    legacy = _call(hub_http, "hello", enroll_other_spoke_dir, "spoke-b")
    assert legacy.status_code == 200 and legacy.json()["credential"] == "missing"
    b_id = machine_payload(enroll_other_spoke_dir, name="spoke-b")["machine_id"]

    monkeypatch.setenv(MODE_ENV, "enforce")
    bearer_a = f"Bearer {credential_a}"
    blocked = _call(hub_http, "status", enroll_spoke_dir, "spoke-a", bearer_a)
    assert blocked.status_code == 503, blocked.text
    assert _code(blocked) == "SYNC_ENFORCE_NOT_ACTIVE"
    assert "1 unowned" in blocked.json()["detail"]["message"]
    assert b_id not in blocked.text, "the refusal must not hand the blocker's id to the caller"

    fleet_admin.adopt_by_email(enroll_hub_dir, machine_id=b_id, owner_email=ENROLL_OWNER_EMAIL)
    still = _call(hub_http, "status", enroll_spoke_dir, "spoke-a", bearer_a)
    assert still.status_code == 503 and "1 no_credential" in still.json()["detail"]["message"], (
        "adopt records an owner but mints no credential, so spoke-b would be "
        f"locked out and ENFORCE must still refuse to activate: {still.text}"
    )

    _b_again, credential_b = _enroll(hub_http, enroll_hub_dir, enroll_other_spoke_dir, "spoke-b")
    assert credential_b is not None, "enroll must mint for an owned machine holding none"
    assert fleet_admin.credentials(enroll_hub_dir)["enforce_ready"] is True
    for data_dir, name, credential in (
        (enroll_spoke_dir, "spoke-a", credential_a),
        (enroll_other_spoke_dir, "spoke-b", credential_b),
    ):
        active = _call(hub_http, "status", data_dir, name, f"Bearer {credential}")
        assert active.status_code == 200, f"{name} once ENFORCE is active: {active.text}"


def test_an_unknown_mode_value_fails_loudly(
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if MDT_SYNC_CREDENTIAL_MODE=on is quietly read as some mode instead of a 500 then broken"""
    _machine_id, credential = _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")
    monkeypatch.setenv(MODE_ENV, "on")
    response = _call(hub_http, "status", enroll_spoke_dir, "spoke-a", f"Bearer {credential}")
    assert response.status_code == 500 and _code(response) == "SYNC_CREDENTIAL_MODE"
    monkeypatch.setenv(MODE_ENV, "ENFORCE")
    assert _call(hub_http, "status", enroll_spoke_dir, "spoke-a", None).status_code == 401, (
        "control: the documented spelling, in any case, is a real mode"
    )


# ----- OBSERVE ---------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_observe_refuses_nothing_and_hello_reports_the_verdict(
    endpoint: str,
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if OBSERVE refuses a caller main accepted, or hello misreports the verdict, then broken"""
    monkeypatch.delenv(MODE_ENV, raising=False)
    assert machine_credentials.configured_mode() == "observe"
    _machine_id, credential = _enroll(hub_http, enroll_hub_dir, enroll_spoke_dir, "spoke-a")
    for authorization, verdict in (
        (None, "missing"),
        (f"Bearer {CREDENTIAL_PREFIX}forged", "invalid"),
        (f"Bearer {credential}", "valid"),
    ):
        response = _call(hub_http, endpoint, enroll_spoke_dir, "spoke-a", authorization)
        assert response.status_code == 200, f"{endpoint}, {verdict}: {response.text}"
        if endpoint == "hello":
            assert response.json()["credential"] == verdict


# ----- minting and the spoke's file -------------------------------------------


def test_enroll_returns_the_credential_once_and_the_hub_keeps_only_its_hash(
    hub_http: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if a replayed enroll gets a credential, or the hub stores more than its hash, then broken"""
    transport = TestClientTransport(hub_http)
    token = mint_grant(enroll_hub_dir)
    first = http_enroll(transport, enroll_spoke_dir, name="spoke-a", token=token)
    credential = first["sync_credential"]
    assert isinstance(credential, str) and credential.startswith(CREDENTIAL_PREFIX)
    stored = read_hub(enroll_hub_dir, "machine_credentials")
    assert [row["credential_sha256"] for row in stored] == [
        hashlib.sha256(credential.encode("utf-8")).hexdigest()
    ]
    assert credential not in repr(stored)

    replay = http_enroll(transport, enroll_spoke_dir, name="spoke-a", token=token)
    assert replay["created"] is False and replay["sync_credential"] is None
    assert read_hub(enroll_hub_dir, "machine_credentials") == stored

    rotated = http_enroll(
        transport, enroll_spoke_dir, name="spoke-a", token=mint_grant(enroll_hub_dir)
    )["sync_credential"]
    assert rotated is not None and rotated != credential, "a FRESH grant rotates"
    monkeypatch.setenv(MODE_ENV, "enforce")
    old = _call(hub_http, "status", enroll_spoke_dir, "spoke-a", f"Bearer {credential}")
    new = _call(hub_http, "status", enroll_spoke_dir, "spoke-a", f"Bearer {rotated}")
    assert (old.status_code, new.status_code) == (401, 200)


def test_the_cli_stores_the_credential_0600_and_sync_sends_it_over_real_http(
    enroll_live_hub: str,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if CLI enroll leaves no 0600 file, or CLI sync does not send it under ENFORCE, then broken"""
    assert isinstance(maintenance.maintenance_enroll._transport_for(enroll_live_hub), HttpTransport)
    argv = ["--data-dir", str(enroll_spoke_dir), "--hub", enroll_live_hub, "--name", "spoke-a"]
    assert maintenance.main(["enroll", *argv, "--grant", mint_grant(enroll_hub_dir)]) == 0
    printed = capsys.readouterr().out
    path = spoke_credential.credential_path(enroll_spoke_dir)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "sync credential issued" in printed
    assert path.read_text(encoding="utf-8") not in printed, "the credential must never be printed"

    monkeypatch.setenv(MODE_ENV, "enforce")
    assert maintenance.main(["sync", *argv]) == 0, capsys.readouterr().out

    machine_id = machine_payload(enroll_spoke_dir, name="spoke-a")["machine_id"]
    with pytest.raises(SyncTransportError, match="HTTP 401"):
        HttpTransport(enroll_live_hub).get(f"{SYNC}/status", {"machine_id": machine_id})

    os.chmod(path, 0o644)
    with pytest.raises(spoke_credential.SpokeCredentialError, match="0644"):
        maintenance.main(["sync", *argv])


def test_the_credential_table_is_hub_local_and_documented() -> None:
    """if machine_credentials rides the sync set or opens a DB without column docs then broken"""
    assert _V11 in state_schema.MIGRATIONS
    assert "machine_credentials" in state_schema.TABLES
    # SYNC_TABLES holds TableSpec objects, so compare NAMES: a bare string
    # `in` against it is always False and could never fail. Positive control:
    # the same name set does contain a table that really syncs.
    synced_names = {spec.name for spec in protocol.SYNC_TABLES}
    assert "tracks" in synced_names and "tracks" in protocol.DIGEST_TABLES
    assert "machine_credentials" not in synced_names
    assert "machine_credentials" not in protocol.DIGEST_TABLES
    assert set(COLUMN_DOCS["machine_credentials"]) == {
        "machine_id",
        "credential_sha256",
        "minted_at",
    }
