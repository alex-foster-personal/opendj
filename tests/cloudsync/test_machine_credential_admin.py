"""The fleet verbs (adopt, revoke, credentials): HTTP twins and CLI parity.

The HTTP routes in ``apps/webui/server/routes/cloudsync_fleet.py`` and the
CLI subcommands in ``apps/sync_hub/fleet_admin.py`` call the same functions;
these tests hold them to the same rows and the same readout, over the real
router, a real signed-in session row, and a real migrated hub DB.

Regression one-liners:
  - if a fleet route serves a caller with no session then broken
  - if a fleet route writes owner rows on an install that is not a hub then broken
  - if HTTP adopt and CLI adopt leave different owner rows then broken
  - if a signed-in user can revoke a machine they do not own then broken
  - if the HTTP and CLI credentials readouts differ, or either shows a token, then broken

[if] fleet adopt/revoke run over HTTP/CLI [then] both write one row, no token shown, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import maintenance
from apps.sync_hub.client_transport_ops import state_db_path
from apps.webui.server.auth import SESSION_COOKIE_NAME, GoogleIdentity, SessionStore
from apps.webui.server.routes import cloudsync_fleet

from .conftest import (
    ENROLL_OTHER_EMAIL,
    ENROLL_OTHER_SUB,
    ENROLL_OWNER_EMAIL,
    ENROLL_OWNER_SUB,
    _enroll_hub_app,
)
from .enrollment_helpers import http_enroll, machine_payload, masked_owner, mint_grant, owner_rows
from .enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("CAT-04")

FLEET: str = "/api/v1/cloudsync/fleet"


@pytest.fixture
def admin(enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The sync router plus the fleet router, on a hub (MDT_IS_HUB=1)."""
    monkeypatch.setenv(machine_identity.IS_HUB_ENV, "1")
    app = _enroll_hub_app(enroll_hub_dir)
    app.include_router(cloudsync_fleet.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield http


def _sign_in(http: TestClient, hub_dir: Path, *, sub: str, email: str) -> None:
    """A real auth_sessions row through the real SessionStore, planted as the cookie."""
    token = SessionStore(state_db_path(hub_dir)).sign_in(
        GoogleIdentity(
            google_sub=sub,
            email=email,
            name=None,
            avatar_url=None,
            refresh_token=None,
            access_token="not-used-by-these-routes",
            access_expires_at="2026-09-11T00:00:00+00:00",
        )
    )
    http.cookies.set(SESSION_COOKIE_NAME, token)


def _hello(http: TestClient, data_dir: Path, name: str) -> str:
    """Register a machine the pre-v9 way: hello only, so it is unowned."""
    machine = machine_payload(data_dir, name=name)
    body = {"machine": machine, "schema_version": state_schema.SCHEMA_VERSION}
    assert http.post("/api/v1/sync/hello", json=body).status_code == 200
    return str(machine["machine_id"])


def test_every_fleet_route_needs_a_signed_in_user(
    admin: TestClient, enroll_hub_dir: Path, enroll_spoke_dir: Path
) -> None:
    """if a fleet route serves a caller with no session then broken"""
    machine_id = _hello(admin, enroll_spoke_dir, "spoke-a")
    for method, path, body in (
        ("GET", f"{FLEET}/credentials", None),
        ("POST", f"{FLEET}/adopt", {"machine_id": machine_id}),
        ("POST", f"{FLEET}/revoke", {"machine_id": machine_id}),
    ):
        response = admin.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path}: {response.text}"
        assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
    assert owner_rows(enroll_hub_dir) == [], "a refused adopt must write nothing"

    _sign_in(admin, enroll_hub_dir, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    assert admin.get(f"{FLEET}/credentials").status_code == 200, "control: signed in"


def test_fleet_routes_refuse_on_an_install_that_is_not_a_hub(
    admin: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if a fleet route writes owner rows on an install that is not a hub then broken"""
    machine_id = _hello(admin, enroll_spoke_dir, "spoke-a")
    _sign_in(admin, enroll_hub_dir, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    monkeypatch.delenv(machine_identity.IS_HUB_ENV)
    response = admin.post(f"{FLEET}/adopt", json={"machine_id": machine_id})
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "CLOUDSYNC_NOT_A_HUB"
    )
    assert owner_rows(enroll_hub_dir) == []


def test_http_adopt_claims_for_the_signed_in_user_exactly_as_the_cli_does(
    admin: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
) -> None:
    """if HTTP and CLI adopt leave different rows, or HTTP adopts for anyone else, then broken"""
    via_http = _hello(admin, enroll_spoke_dir, "spoke-a")
    via_cli = _hello(admin, enroll_other_spoke_dir, "spoke-b")
    _sign_in(admin, enroll_hub_dir, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)

    response = admin.post(f"{FLEET}/adopt", json={"machine_id": via_http})
    assert response.status_code == 200, response.text
    assert response.json()["owner_email"] == ENROLL_OWNER_EMAIL
    assert response.json()["enrolled_via"] == "adopt" and response.json()["created"] is True
    assert (
        maintenance.main(
            [
                "adopt",
                "--data-dir",
                str(enroll_hub_dir),
                "--machine-id",
                via_cli,
                "--owner",
                ENROLL_OWNER_EMAIL,
            ]
        )
        == 0
    )

    by_machine = {row["machine_id"]: row for row in owner_rows(enroll_hub_dir)}
    assert set(by_machine) == {via_http, via_cli}
    assert masked_owner(by_machine[via_http]) == masked_owner(by_machine[via_cli])
    assert by_machine[via_http]["google_sub"] == ENROLL_OWNER_SUB

    unknown = admin.post(f"{FLEET}/adopt", json={"machine_id": "0" * 32})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "CLOUDSYNC_UNKNOWN_MACHINE"


def test_http_revoke_only_revokes_the_callers_own_machines(
    admin: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
) -> None:
    """if a user revokes a machine they do not own, or revoke keeps the credential, then broken"""
    body = http_enroll(
        TestClientTransport(admin),
        enroll_spoke_dir,
        name="spoke-a",
        token=mint_grant(enroll_hub_dir),
    )
    machine_id = str(body["machine_id"])
    unowned = _hello(admin, enroll_other_spoke_dir, "spoke-b")

    _sign_in(admin, enroll_hub_dir, sub=ENROLL_OTHER_SUB, email=ENROLL_OTHER_EMAIL)
    refused = admin.post(f"{FLEET}/revoke", json={"machine_id": machine_id})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "CLOUDSYNC_NOT_YOUR_MACHINE"
    assert [row["revoked_at"] for row in owner_rows(enroll_hub_dir)] == [None]

    _sign_in(admin, enroll_hub_dir, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    revoked = admin.post(f"{FLEET}/revoke", json={"machine_id": machine_id})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["changed"] is True and revoked.json()["credential_deleted"] is True
    assert [row["revoked_at"] for row in owner_rows(enroll_hub_dir)] == [
        revoked.json()["revoked_at"]
    ]

    again = admin.post(f"{FLEET}/revoke", json={"machine_id": machine_id})
    assert again.status_code == 200 and again.json()["changed"] is False
    assert again.json()["revoked_at"] == revoked.json()["revoked_at"], "a no-op keeps the stamp"

    nothing = admin.post(f"{FLEET}/revoke", json={"machine_id": unowned})
    assert nothing.status_code == 409 and nothing.json()["detail"]["code"] == "CLOUDSYNC_FLEET"


def test_the_http_credentials_readout_equals_the_cli_json(
    admin: TestClient,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """if the HTTP and CLI credentials readouts differ, or either shows a token, then broken"""
    credential = http_enroll(
        TestClientTransport(admin),
        enroll_spoke_dir,
        name="spoke-a",
        token=mint_grant(enroll_hub_dir),
    )["sync_credential"]
    blocker = _hello(admin, enroll_other_spoke_dir, "spoke-b")
    _sign_in(admin, enroll_hub_dir, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)

    response = admin.get(f"{FLEET}/credentials")
    assert response.status_code == 200, response.text
    capsys.readouterr()
    assert maintenance.main(["credentials", "--data-dir", str(enroll_hub_dir), "--json"]) == 0
    printed = capsys.readouterr().out
    assert response.json() == json.loads(printed)

    readout = response.json()
    assert readout["mode"] == "observe" and readout["enforce_ready"] is False
    assert readout["blockers"] == [{"machine_id": blocker, "name": "spoke-b", "reason": "unowned"}]
    assert str(credential) not in response.text and str(credential) not in printed
