"""ADR 12 enrollment: OWNERSHIP as the hub reports it, and as it holds up.

The other half -- what one enrollment writes and what it refuses -- is in
``test_enrollment.py`` beside this file. This module owns everything that
reads ownership back: the hub-binding guard, the ``fleet`` readout, what
``hello`` discloses (and deliberately does not), revocation, and a grant that
has already been spent.

The hub-binding guard is the load-bearing one. ``machine_owners`` rows carry
``hub_machine_id`` and a hub restored from another machine's backup inherits
the whole table but NOT its ``machine-id`` file, which lives outside the DB
(ADR 05 section 1). A row stamped by a different hub must therefore read as
FOREIGN rather than as ownership, and it must be REPORTED rather than
deleted: the hub that wrote it may still be alive.

Acceptance criteria, one test each:
- if an owner row stamped by ANOTHER hub is honored here then broken
- if a foreign row is silently deleted rather than reported then broken
- if hello discloses the owner's email to an unauthenticated caller then broken
- if a revoked machine re-enrolls without a fresh grant then broken
- if a spent grant keeps working past its expiry then broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, enrollment, enrollment_credentials, service, wire_version
from tests.cloudsync.conftest import (
    ENROLL_OTHER_EMAIL,
    ENROLL_OTHER_SUB,
    ENROLL_OWNER_EMAIL,
    ENROLL_OWNER_SUB,
    HELLO_PATH,
    seed_user,
)
from tests.cloudsync.enrollment_helpers import (
    http_enroll,
    hub_machine_id,
    machine_payload,
    mint_grant,
    owner_rows,
    read_hub,
)
from tests.cloudsync.enrollment_transport import TestClientTransport

# ----- the hub binding guard ----------------------------------------------


def _live_owner(hub_dir: Path, machine_id: str) -> enrollment.OwnerIdentity | None:
    from apps.shared.state import db as state_db

    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        return enrollment.owner_for(
            conn, machine_id, hub_machine_id=hub_machine_id(hub_dir)
        )
    finally:
        conn.close()


def _restamp_owner_hub(hub_dir: Path, machine_id: str, value: str) -> None:
    from apps.shared.state import db as state_db

    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        conn.execute(
            "UPDATE machine_owners SET hub_machine_id = ? WHERE machine_id = ?",
            (value, machine_id),
        )
    finally:
        conn.close()


def test_an_owner_row_stamped_by_this_hub_is_honored(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The control that could fail: same row, this hub's id, must be honored."""
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    owner = _live_owner(enroll_hub_dir, machine_id)
    assert owner is not None
    assert owner.google_sub == ENROLL_OWNER_SUB


def test_an_owner_row_stamped_by_another_hub_is_not_honored(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    _restamp_owner_hub(enroll_hub_dir, machine_id, "f" * 32)
    assert _live_owner(enroll_hub_dir, machine_id) is None


def test_a_foreign_owner_row_is_reported_not_deleted(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    from apps.shared.state import db as state_db

    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    _restamp_owner_hub(enroll_hub_dir, machine_id, "f" * 32)

    conn = state_db.open_rw(client.state_db_path(enroll_hub_dir))
    try:
        fleet = enrollment.fleet_ownership(
            conn, hub_machine_id=hub_machine_id(enroll_hub_dir)
        )
    finally:
        conn.close()
    states = {row.machine_id: row.state for row in fleet}
    assert states[machine_id] == "foreign"
    assert owner_rows(enroll_hub_dir), (
        "the evidence row must survive being disbelieved"
    )


def test_fleet_ownership_names_the_unowned_machines(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """A machine that only ever said hello reads unowned, never owned."""
    from apps.shared.state import db as state_db

    enroll_hub.post(
        HELLO_PATH,
        {
            "machine": machine_payload(enroll_spoke_dir, name="never-enrolled"),
            "schema_version": state_schema.SCHEMA_VERSION,
            "machines": [],
        },
    )
    conn = state_db.open_rw(client.state_db_path(enroll_hub_dir))
    try:
        fleet = enrollment.fleet_ownership(
            conn, hub_machine_id=hub_machine_id(enroll_hub_dir)
        )
    finally:
        conn.close()
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    states = {row.machine_id: row.state for row in fleet}
    assert states[machine_id] == "unowned"
    assert states[hub_machine_id(enroll_hub_dir)] == "unowned"


def test_hello_reports_the_callers_ownership(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """OBSERVE mode: hello answers "am I owned?" without refusing anybody."""
    payload = {
        "machine": machine_payload(enroll_spoke_dir, name="nucbox-wsl"),
        "schema_version": state_schema.SCHEMA_VERSION,
        "machines": [],
    }
    before = enroll_hub.post(HELLO_PATH, payload)
    assert before["ownership"] == "unowned"

    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    after = enroll_hub.post(HELLO_PATH, payload)
    assert after["ownership"] == "owned"


def test_hello_does_not_disclose_the_owners_email(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """An unauthenticated caller learns the STATE, never the account.

    ``hello`` reports on the machine_id in the caller's own payload and hands
    back every machine_id this hub knows, so an email here would be readable
    by anything that can open a socket, for the whole fleet.

    The control is in the same test and it fires on the same data: the ENROLL
    response, which is authenticated by a credential, DOES carry the email. So
    a probe that cannot see ``owner_email`` anywhere would fail this test
    rather than pass it.
    """
    enrolled = http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    assert enrolled["owner_email"] == ENROLL_OWNER_EMAIL, (
        "control: the credentialed response is where the email belongs"
    )
    body = enroll_hub.post(
        HELLO_PATH,
        {
            "machine": machine_payload(enroll_spoke_dir, name="nucbox-wsl"),
            "schema_version": state_schema.SCHEMA_VERSION,
            "machines": [],
        },
    )
    assert body["ownership"] == "owned", "the state is still reported"
    assert "owner_email" not in body
    assert ENROLL_OWNER_EMAIL not in str(body), (
        "and not under some other key either"
    )
    assert "owner_email" not in service.HelloResponse.model_fields


# ----- a revocation, and a foreign row somebody else wants ------------------


def _set_owner_column(hub_dir: Path, machine_id: str, column: str, value: str) -> None:
    """Hand-write one ``machine_owners`` column. No app path writes revoked_at."""
    from apps.shared.state import db as state_db

    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        conn.execute(
            f"UPDATE machine_owners SET {column} = ? WHERE machine_id = ?",
            (value, machine_id),
        )
    finally:
        conn.close()


def test_enrolling_a_revoked_machine_is_refused_and_the_revocation_stands(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """A re-enroll must not lift a revocation as a side effect.

    Paired with the control below: identical calls, the revocation the only
    difference between the two arms.
    """
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    _set_owner_column(
        enroll_hub_dir, machine_id, "revoked_at", "2026-09-01T00:00:00.000000+00:00"
    )

    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(
            enroll_hub,
            enroll_spoke_dir,
            name="nucbox-wsl",
            token=mint_grant(enroll_hub_dir),
        )
    assert "409" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir)[0]["revoked_at"] is not None, (
        "the revocation must survive the enroll that tried to overwrite it"
    )


def test_the_same_re_enroll_succeeds_when_the_row_is_not_revoked(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The control for the revocation refusal: one variable, opposite answer."""
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    body = http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    assert body["created"] is False
    assert owner_rows(enroll_hub_dir)[0]["revoked_at"] is None


def test_a_foreign_owner_row_is_not_taken_over_by_a_different_user(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """A restored hub must not become a way to claim somebody else's machine.

    On a restored hub EVERY owner row is foreign, so a conflict guard that
    only fires for rows this hub wrote is absent in precisely the situation
    it exists for.
    """
    seed_user(
        client.state_db_path(enroll_hub_dir),
        sub=ENROLL_OTHER_SUB,
        email=ENROLL_OTHER_EMAIL,
    )
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    _restamp_owner_hub(enroll_hub_dir, machine_id, "f" * 32)

    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(
            enroll_hub,
            enroll_spoke_dir,
            name="nucbox-wsl",
            token=mint_grant(enroll_hub_dir, email=ENROLL_OTHER_EMAIL),
        )
    assert "409" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir)[0]["google_sub"] == ENROLL_OWNER_SUB, (
        "the foreign row still names its own owner, not the claimant"
    )


def test_the_foreign_rows_own_owner_can_re_enroll_over_it(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The control, and the documented resolution: same call, same owner.

    Without it the refusal above is equally satisfied by an endpoint that
    refuses every foreign row, which would leave a restored hub with no way
    back at all.
    """
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    _restamp_owner_hub(enroll_hub_dir, machine_id, "f" * 32)

    body = http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    assert body["created"] is True
    assert owner_rows(enroll_hub_dir)[0]["hub_machine_id"] == hub_machine_id(
        enroll_hub_dir
    )


# ----- a spent grant is not a standing key ---------------------------------


def _expire_grant(hub_dir: Path, token: str) -> None:
    from apps.shared.state import db as state_db

    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        conn.execute(
            "UPDATE enrollment_grants SET expires_at = ? "
            "WHERE grant_token_sha256 = ?",
            (
                "2026-09-01T00:00:00.000000+00:00",
                enrollment_credentials.hash_grant_token(token),
            ),
        )
    finally:
        conn.close()


def test_a_spent_grant_stops_working_once_it_expires(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """Redemption must not exempt a grant from its own TTL.

    A spent grant that never expires is a standing key, and on the dev path
    "spent" is the state the token spends its whole life in, sitting in the
    joining machine's shell history.
    """
    token = mint_grant(enroll_hub_dir)
    http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    _expire_grant(enroll_hub_dir, token)

    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(
            enroll_hub, enroll_spoke_dir, name="renamed-by-the-old-token", token=token
        )
    assert "401" in str(excinfo.value)
    machines = {row["machine_id"]: row for row in read_hub(enroll_hub_dir, "machines")}
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    assert machines[machine_id]["name"] == "nucbox-wsl", (
        "an expired grant must not still be able to rewrite the fleet row"
    )


def test_the_same_spent_grant_still_works_before_it_expires(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The control: identical re-presentation, expiry the only difference."""
    token = mint_grant(enroll_hub_dir)
    http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    body = http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    assert body["created"] is False


# ----- the contract says what the endpoint actually answers with -------------


def test_an_omitted_ownership_fails_loudly_instead_of_reading_as_unowned() -> None:
    """[if] HelloResponse can be built without an ownership state then the
    hub can report a value it never measured, [else stop].

    Sol review, PR #1648, P1 BLOCKING. The field carried a ``"unowned"``
    default, which is a real measured state -- so a response the hub failed
    to compute would have been indistinguishable from a machine it looked at
    and found unowned, at an identity boundary.

    Asserted through a CONSTRUCTION that omits the field rather than by
    reading ``model_fields[...].is_required()``: the second passes on a model
    whose validation is switched off, and it is validation, not the metadata,
    that has to refuse.
    """
    import pydantic

    common = {
        "hub_machine_id": "hub-1",
        "schema_version": state_schema.SCHEMA_VERSION,
        "wire_version": wire_version.WIRE_VERSION,
        "seq": 0,
        "machines": [],
        "hub_generation": "gen-1",
        "capabilities": [],
        # Required since plan X5, like ownership; supplied so the refusal
        # below can still only be about the missing ownership field.
        "credential": "missing",
    }
    with pytest.raises(pydantic.ValidationError, match="ownership"):
        service.HelloResponse(**common)

    covered = service.HelloResponse(**common, ownership="unowned")
    assert covered.ownership == "unowned", (
        "control: the same construction WITH the state is accepted, so the "
        "refusal above is about the missing field and not about the rest of "
        "this payload being wrong"
    )


def test_the_enroll_route_declares_the_statuses_it_really_returns() -> None:
    """[if] the OpenAPI document omits a status the enroll route raises then
    a generated client cannot branch on it, [else stop].

    Sol review, PR #1648, P1 BLOCKING. Derived from the source rather than
    from a copied list: the expectation is every ``status_code=`` the
    enrollment mapping raises, read out of the module, so a fourth refusal
    added later fails here instead of silently going undeclared.
    """
    import ast
    import inspect

    from apps.engine_core.app import create_app
    from apps.engine_core.config import EngineConfig
    from apps.sync_hub import service_enroll

    tree = ast.parse(inspect.getsource(service_enroll))
    raised: set[int] = {
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "HTTPException"
        for kw in node.keywords
        if kw.arg == "status_code"
        and isinstance(kw.value, ast.Constant)
        # ``ast.Constant.value`` is any literal, so narrow to the ints here
        # rather than sorting a union later: a non-int status_code is not a
        # thing this scan should quietly carry into the comparison.
        and isinstance(kw.value.value, int)
    }
    assert raised, (
        "control: the scan found no HTTPException status codes at all in "
        "the enrollment mapping, which would make the comparison below "
        "vacuously true"
    )

    declared = {code for code in service_enroll.ENROLL_RESPONSES if isinstance(code, int)}
    assert raised <= declared, (
        f"the enroll mapping raises {sorted(raised - declared)} that "
        "ENROLL_RESPONSES does not declare"
    )

    import tempfile

    cfg = EngineConfig(data_dir=Path(tempfile.mkdtemp(prefix="enroll-oapi-")))
    spec = create_app(cfg).openapi()
    responses = spec["paths"]["/api/v1/sync/enroll"]["post"]["responses"]
    assert raised <= {int(code) for code in responses}, (
        "the generated document is missing statuses the route raises: "
        f"{sorted(raised - {int(code) for code in responses})}"
    )
