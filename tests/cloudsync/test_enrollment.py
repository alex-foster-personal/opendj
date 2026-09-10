"""ADR 12 enrollment: the MECHANISM. What one enrollment writes, and refuses.

The other half -- ownership as the hub REPORTS it back, and what happens to a
row after it exists -- is in ``test_enrollment_ownership.py`` beside this
file. Split for the quality-gate file_size ratchet, along the seam the two
halves already had: this module drives ``POST /api/v1/sync/enroll`` and reads
the rows it wrote; that one reads ``hello``, ``fleet`` and the hub binding.

Every refusal here carries its own NEGATIVE CONTROL: a request of the same
shape that SHOULD succeed, run against the same fixture. A refusal test that
passes because the endpoint refuses everything is the failure mode this
guards, and it cannot be ruled out by the refusal alone.

Acceptance criteria, one test each:
- if a successful enrollment does not write exactly one owned row then broken
- if a second enrollment of the same machine writes a second row then broken
- if an unknown, expired or already-redeemed grant enrolls anything then broken
- if the owner comes from the REQUEST rather than from the grant then broken
- if the raw grant token reaches the database then broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import client, enrollment_credentials, service
from tests.cloudsync.conftest import (
    ENROLL_OTHER_EMAIL,
    ENROLL_OTHER_SUB,
    ENROLL_OWNER_EMAIL,
    ENROLL_OWNER_SUB,
    seed_user,
)
from tests.cloudsync.enrollment_helpers import (
    http_enroll,
    hub_machine_id,
    mint_expired_grant,
    mint_grant,
    owner_rows,
    read_hub,
)
from tests.cloudsync.enrollment_transport import TestClientTransport

# ----- the row a successful enrollment writes ------------------------------


def test_enrolling_a_new_machine_writes_exactly_one_owned_row(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    token = mint_grant(enroll_hub_dir)
    body = http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)

    rows = owner_rows(enroll_hub_dir)
    assert len(rows) == 1, "one enrollment is one row, not zero and not two"
    row = rows[0]
    assert row["machine_id"] == machine_identity.get_or_create_machine_id(
        enroll_spoke_dir
    )
    assert row["google_sub"] == ENROLL_OWNER_SUB
    assert row["hub_machine_id"] == hub_machine_id(enroll_hub_dir)
    assert row["enrolled_via"] == "grant"
    assert row["revoked_at"] is None
    assert body["created"] is True
    assert body["owner_email"] == ENROLL_OWNER_EMAIL


def test_the_enrolled_machine_is_registered_in_the_fleet(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """Enrollment REGISTERS as well as owns: one call, one transaction."""
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    names = {row["name"] for row in read_hub(enroll_hub_dir, "machines")}
    assert "nucbox-wsl" in names, "enroll must leave a machines row behind"


# ----- idempotency ---------------------------------------------------------


def test_enrolling_twice_is_a_no_op(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The dev path is re-runnable: the second run changes nothing at all."""
    token = mint_grant(enroll_hub_dir)
    first = http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    after_first = owner_rows(enroll_hub_dir)

    second = http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    after_second = owner_rows(enroll_hub_dir)

    assert first["created"] is True
    assert second["created"] is False, (
        "a re-run must report that it changed nothing, not claim a fresh enrollment"
    )
    assert after_second == after_first, (
        "re-enrolling rewrote the row; enrolled_at and enrolled_via are the "
        "audit trail of WHEN the machine actually joined"
    )
    assert len(after_second) == 1


def test_enrolling_twice_with_two_grants_is_still_one_row(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """Idempotency is keyed on the machine, not on the credential."""
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    before = owner_rows(enroll_hub_dir)
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    assert owner_rows(enroll_hub_dir) == before


# ----- refusals, and the negative control on each --------------------------


def test_an_unknown_grant_is_refused_and_writes_nothing(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    machines_before = read_hub(enroll_hub_dir, "machines")
    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(
            enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token="not-a-real-grant"
        )
    assert "401" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir) == []
    assert read_hub(enroll_hub_dir, "machines") == machines_before, (
        "a refused enrollment must not register the machine either"
    )


def test_a_grant_redeemed_by_one_machine_cannot_enroll_another(
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
):
    token = mint_grant(enroll_hub_dir)
    http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(enroll_hub, enroll_other_spoke_dir, name="second", token=token)
    assert "401" in str(excinfo.value)
    rows = owner_rows(enroll_hub_dir)
    assert len(rows) == 1, "single use means a second MACHINE, never a second row"
    assert rows[0]["machine_id"] == machine_identity.get_or_create_machine_id(
        enroll_spoke_dir
    )


def test_an_expired_grant_enrolls_nothing(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    token = mint_expired_grant(enroll_hub_dir)
    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    assert "401" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir) == []


def test_a_live_grant_of_the_same_shape_does_enroll(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The control for the expiry test: same call, TTL the only difference.

    Without it, that refusal is equally satisfied by an endpoint which
    refuses everything.
    """
    token = mint_grant(enroll_hub_dir, ttl_s=900)
    http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=token)
    assert len(owner_rows(enroll_hub_dir)) == 1


def test_the_owner_comes_from_the_grant_not_the_request(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """A request cannot name whose machine it is becoming."""
    seed_user(
        client.state_db_path(enroll_hub_dir),
        sub=ENROLL_OTHER_SUB,
        email=ENROLL_OTHER_EMAIL,
    )
    token = mint_grant(enroll_hub_dir, email=ENROLL_OWNER_EMAIL)
    body = http_enroll(
        enroll_hub,
        enroll_spoke_dir,
        name="nucbox-wsl",
        token=token,
        extra={"google_sub": ENROLL_OTHER_SUB, "owner_email": ENROLL_OTHER_EMAIL},
    )
    assert body["owner_google_sub"] == ENROLL_OWNER_SUB
    assert owner_rows(enroll_hub_dir)[0]["google_sub"] == ENROLL_OWNER_SUB
    assert "google_sub" not in service.EnrollRequest.model_fields, (
        "the request model must have no owner field at all, so there is "
        "nothing for a caller to set"
    )


def test_re_enrolling_under_a_different_user_is_refused(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    seed_user(
        client.state_db_path(enroll_hub_dir),
        sub=ENROLL_OTHER_SUB,
        email=ENROLL_OTHER_EMAIL,
    )
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(
            enroll_hub,
            enroll_spoke_dir,
            name="nucbox-wsl",
            token=mint_grant(enroll_hub_dir, email=ENROLL_OTHER_EMAIL),
        )
    assert "409" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir)[0]["google_sub"] == ENROLL_OWNER_SUB, (
        "a transfer must be explicit; it must not ride an ordinary enroll"
    )


# ----- the grant itself ----------------------------------------------------


def test_the_raw_grant_token_never_reaches_the_database(enroll_hub_dir: Path):
    token = mint_grant(enroll_hub_dir)
    stored = [
        row["grant_token_sha256"] for row in read_hub(enroll_hub_dir, "enrollment_grants")
    ]
    assert len(stored) == 1
    assert token not in stored
    assert stored[0] == enrollment_credentials.hash_grant_token(token), (
        "the control: the HASH of the raw token IS what was stored, so this "
        "test cannot pass by the row simply being absent"
    )


def test_minting_a_grant_for_an_unknown_email_fails_loudly(enroll_hub_dir: Path):
    from apps.sync_hub import maintenance_enroll

    with pytest.raises(enrollment_credentials.EnrollmentCredentialError) as excinfo:
        maintenance_enroll.grant(enroll_hub_dir, owner_email="nobody@example.com")
    assert "nobody@example.com" in str(excinfo.value)


def test_the_google_id_token_kind_is_declared_and_not_yet_resolvable(
    enroll_hub: TestClientTransport, enroll_hub_dir: Path, enroll_spoke_dir: Path
):
    """The USER path's credential slot exists and says so, rather than 401ing.

    A reserved kind answering 401 would be indistinguishable from a bad
    credential, and the seam would be invisible to whoever picks up the
    in-app work.
    """
    assert "google_id_token" in enrollment_credentials.CREDENTIAL_KINDS
    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(
            enroll_hub,
            enroll_spoke_dir,
            name="nucbox-wsl",
            token="an.id.token",
            kind="google_id_token",
        )
    assert "501" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir) == []


# ----- what a grant's lifetime, and a re-run, are allowed to do --------------


@pytest.mark.parametrize("ttl_s", [0, -1, enrollment_credentials.GRANT_TTL_MAX_S + 1])
def test_a_grant_cannot_be_minted_outside_its_bounded_lifetime(
    enroll_hub_dir: Path, ttl_s: int
) -> None:
    """[if] a grant can be minted with an unbounded lifetime then a leaked
    token is a standing key, [else stop].

    Sol review, PR #1648, P1 BLOCKING. ``ttl_s`` was taken on trust, so
    ``grant --ttl-seconds 99999999`` minted a credential valid for years
    while every docstring kept calling it short-lived.

    Asserted at :func:`mint_grant`, the only writer of the grants table,
    rather than on the argparse argument: a check on one caller leaves the
    HTTP and programmatic callers unbounded.
    """
    conn = state_db.open_rw(client.state_db_path(enroll_hub_dir))
    try:
        owner = enrollment_credentials.owner_by_email(conn, ENROLL_OWNER_EMAIL)
        with pytest.raises(
            enrollment_credentials.EnrollmentCredentialError, match=str(ttl_s)
        ):
            enrollment_credentials.mint_grant(conn, owner=owner, ttl_s=ttl_s)
        assert not read_hub(enroll_hub_dir, "enrollment_grants"), (
            "a refused mint must leave no row behind"
        )

        minted = enrollment_credentials.mint_grant(
            conn, owner=owner, ttl_s=enrollment_credentials.GRANT_TTL_MAX_S
        )
        assert minted.token, (
            "control: the boundary value itself is ACCEPTED, so the refusals "
            "above are about the bound and not about minting being broken"
        )
    finally:
        conn.close()


def test_a_repeat_enrollment_does_not_quietly_rename_the_fleet_row(
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """[if] a re-run reports "already enrolled" while changing the machines
    row then the report is a lie, [else stop].

    Sol review, PR #1648, P2. ``merge_machines`` ran BEFORE the idempotent
    return, so the caller-supplied row was upserted on every re-run: a second
    enroll under a different ``--name`` renamed the fleet row while the result
    said ``created=False``. Refreshing registry fields is what ``hello`` does
    on every handshake; enroll is about ownership.
    """
    http_enroll(
        enroll_hub,
        enroll_spoke_dir,
        name="original-name",
        token=mint_grant(enroll_hub_dir),
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    before = {row["machine_id"]: row for row in read_hub(enroll_hub_dir, "machines")}
    assert before[machine_id]["name"] == "original-name", (
        "control: the first enrollment really did write the name being "
        "watched, so an unchanged name below is a name that held rather "
        "than one that was never there"
    )

    again = http_enroll(
        enroll_hub,
        enroll_spoke_dir,
        name="renamed-behind-your-back",
        token=mint_grant(enroll_hub_dir),
    )
    assert again["created"] is False, "the re-run is reported as a no-op"

    after = {row["machine_id"]: row for row in read_hub(enroll_hub_dir, "machines")}
    assert after[machine_id] == before[machine_id], (
        "a re-run reported as created=False changed the fleet row: "
        f"{before[machine_id]} -> {after[machine_id]}"
    )
