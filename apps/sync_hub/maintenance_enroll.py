"""The DEV enrollment path: ``grant`` on the hub, ``enroll`` on the newcomer.

Contract: ``specs/design_decision_12.md`` section C. the maintainer's requirement was a
formalized single method for adding a machine to cloudsync -- "a real command,
not a ritual" -- with the in-app path landing on the same mechanism.

    python -m apps.sync_hub grant  --data-dir DIR --owner EMAIL [--ttl-seconds N]
    python -m apps.sync_hub enroll --data-dir DIR --hub URL [--name N]
                                   (--grant TOKEN | --grant-file FILE)
    python -m apps.sync_hub fleet  --data-dir DIR [--json]

:func:`enroll` is a thin shell over ``POST /api/v1/sync/enroll``. It does NOT
import :mod:`apps.sync_hub.enrollment` and it does NOT open the state
database, and that is the whole point: the agent-native parity rule is
satisfied structurally rather than by discipline, because the CLI has no
implementation of its own that could drift from the endpoint's. The only
thing it reads locally is ``<data-dir>/machine-id``, which is a file and not a
table.

:func:`grant` and :func:`fleet` ARE hub-local, and that is not an exception to
the rule: minting a credential and reading who owns what are acts of hub
authority, so they run where that authority lives. They share one
implementation with their HTTP twins, ``GET /api/v1/cloudsync/fleet`` and
``POST /api/v1/cloudsync/enrollment-grants``
(``apps/webui/server/routes/cloudsync_ops.py``), by being the functions those
routes call -- see this module's ``__all__``.
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import config as sync_config
from apps.sync_hub import (
    enrollment,
    enrollment_credentials,
    protocol,
    service_enroll,
    spoke_credential,
    wire_version,
)
from apps.sync_hub.client_transport_ops import state_db_path
from apps.sync_hub.transport import API_PREFIX, HttpTransport, HubTransport

ENROLL_PATH: str = f"{API_PREFIX}/enroll"


@dataclass(frozen=True)
class EnrollOutcome:
    """What the hub reported back. ``created`` False means nothing changed."""

    machine_id: str
    name: str
    owner_email: str
    hub_machine_id: str
    enrolled_at: str
    enrolled_via: str
    created: bool
    #: True when the hub minted a sync credential on THIS call and it was
    #: written to ``credential_path``. The token itself is never carried here,
    #: so no readout or JSON dump of an outcome can leak it.
    credential_issued: bool
    #: ``<data-dir>/sync-credential`` when a credential is on file after this
    #: call (new or kept from before), None when this machine holds none.
    credential_path: str | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "name": self.name,
            "owner_email": self.owner_email,
            "hub_machine_id": self.hub_machine_id,
            "enrolled_at": self.enrolled_at,
            "enrolled_via": self.enrolled_via,
            "created": self.created,
            "credential_issued": self.credential_issued,
            "credential_path": self.credential_path,
        }


def _transport_for(hub_url: str) -> HubTransport:
    """The real HTTP transport. Its own function so a test can drive the
    in-process router without the CLI knowing, exactly as ``sync`` does."""
    return HttpTransport(hub_url)


def _open_hub(data_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(state_db_path(Path(data_dir)))


# ----- the joining machine's side ------------------------------------------


def local_machine_row(data_dir: Path, *, name: str | None = None) -> protocol.MachineRow:
    """This machine as it will introduce itself. Built from the id FILE.

    No database is opened: ``machine_id`` lives at ``<data-dir>/machine-id``
    precisely so a restored DB cannot inherit another machine's identity
    (ADR 05 section 1), and the rest is platform, hostname and data root.

    Pass ``--name`` explicitly on WSL. ``machines.name`` is UNIQUE and WSL's
    default hostname is frequently the Windows host's, so ``nucbox-wsl`` and a
    native Windows install on the same box would collide on the hub.
    """
    stamp = sync_stamp.canonical_now()
    if name is None:
        name = sync_config.configured_machine_name(Path(data_dir))
    return protocol.MachineRow(
        machine_id=machine_identity.get_or_create_machine_id(Path(data_dir)),
        name=name if name is not None else machine_identity.default_machine_name(),
        platform=machine_identity.detect_platform(),
        is_hub=machine_identity.is_hub_from_env(),
        data_root=str(Path(data_dir)),
        first_seen=stamp,
        last_seen=stamp,
    )


def read_grant_token(*, token: str | None, token_file: Path | None) -> str:
    """The grant to present, from argv or from a file. Exactly one source.

    ``--grant-file -`` reads stdin, which is the form that keeps the token out
    of both the process table and shell history: a value passed as an argument
    is readable from ``/proc`` by any other local process for the duration of
    the call, and on nucbox-wsl that is the joining machine itself.

    Both sources empty, or both set, is a programming error rather than an
    operator error -- argparse's mutually exclusive group already refuses
    those -- so it raises rather than picking one.
    """
    if (token is None) == (token_file is None):
        raise ValueError(
            "exactly one of token / token_file must be given; argparse "
            "enforces this on the CLI, so reaching here means a caller built "
            "the arguments by hand and built them wrong."
        )
    if token_file is not None:
        source = "stdin" if str(token_file) == "-" else str(token_file)
        value = (
            sys.stdin.read()
            if str(token_file) == "-"
            else token_file.read_text(encoding="utf-8")
        )
    else:
        source, value = "the command line", str(token)
    stripped = value.strip()
    if not stripped:
        raise ValueError(
            f"the enrollment grant read from {source} is empty. Mint one on "
            f"the hub with `python -m apps.sync_hub grant --data-dir <hub "
            f"data dir> --owner <email>`."
        )
    return stripped


def enroll(
    data_dir: Path,
    hub_url: str,
    *,
    credential_kind: str,
    credential_value: str,
    name: str | None = None,
    transport: HubTransport | None = None,
) -> EnrollOutcome:
    """Join ``hub_url``'s fleet. Idempotent: a re-run reports ``created`` False.

    ``transport`` exists for the tests that drive the real router in-process;
    the CLI never passes it, so an operator always talks real HTTP. Any
    transport failure propagates unchanged -- no repair, no retry that could
    hide a hub that is answering 401.
    """
    machine = local_machine_row(Path(data_dir), name=name)
    hub = transport if transport is not None else _transport_for(hub_url)
    body = hub.post(
        ENROLL_PATH,
        {
            "machine": machine.to_wire(),
            "schema_version": state_schema.SCHEMA_VERSION,
            "wire_version": wire_version.WIRE_VERSION,
            "credential": {"kind": credential_kind, "value": credential_value},
        },
    )
    # VALIDATED through the endpoint's own response model, not coerced field
    # by field. Sol review, PR #1648 (P1): `bool(body["created"])` turns the
    # string "false" into True, so a hub answering off-contract would have
    # made the CLI print "enrolled" for a machine it did not enroll -- the
    # one line an operator actually reads. `str()` on the rest had the
    # matching flaw, rendering a null as the text "None". One definition,
    # both sides of the wire; a field the server adds is covered here for
    # free instead of needing a second edit.
    reported = service_enroll.EnrollResponse.model_validate(body)
    # Written BEFORE anything is printed, and only when minted: the response
    # is the one time the raw credential exists anywhere.
    if reported.sync_credential is not None:
        spoke_credential.write_credential(Path(data_dir), reported.sync_credential)
    on_file = spoke_credential.credential_path(Path(data_dir))
    return EnrollOutcome(
        machine_id=reported.machine_id,
        name=machine.name,
        owner_email=reported.owner_email,
        hub_machine_id=reported.hub_machine_id,
        enrolled_at=reported.enrolled_at,
        enrolled_via=reported.enrolled_via,
        created=reported.created,
        credential_issued=reported.sync_credential is not None,
        credential_path=str(on_file) if on_file.exists() else None,
    )


# ----- the hub's side ------------------------------------------------------


def grant(
    data_dir: Path,
    *,
    owner_email: str,
    ttl_s: int = enrollment_credentials.GRANT_TTL_S,
) -> enrollment_credentials.MintedGrant:
    """Mint one single-use grant on this hub. The raw token exists once.

    Raises :class:`~apps.sync_hub.enrollment_credentials.EnrollmentCredentialError`
    when no signed-in user has that email. It will not create one: a
    ``users`` row is the record of a real Google sign-in, and inventing one
    would put a fabricated owner on a real machine.
    """
    conn = _open_hub(data_dir)
    try:
        conn.execute("BEGIN")
        try:
            owner = enrollment_credentials.owner_by_email(conn, owner_email)
            minted = enrollment_credentials.mint_grant(conn, owner=owner, ttl_s=ttl_s)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return minted
    finally:
        conn.close()


def fleet(data_dir: Path) -> dict[str, Any]:
    """Who owns what on this hub, plus the counts, as one payload.

    All three counts are always present, zeroes included: an absent key would
    read as "not measured", and this readout is what an operator uses to find
    the machines that still need enrolling.
    """
    hub_machine_id = machine_identity.get_or_create_machine_id(Path(data_dir))
    conn = _open_hub(data_dir)
    try:
        machines = enrollment.fleet_ownership(conn, hub_machine_id=hub_machine_id)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "hub_machine_id": hub_machine_id,
        "machines": [machine.to_wire() for machine in machines],
    }
    payload.update(enrollment.ownership_counts(machines))
    return payload


# ----- human readouts ------------------------------------------------------


def enroll_lines(outcome: EnrollOutcome) -> list[str]:
    """The enroll readout. A no-op must SAY it was a no-op."""
    verb = "enrolled" if outcome.created else "already enrolled"
    if outcome.credential_issued:
        credential = f"sync credential issued and stored at {outcome.credential_path} (0600)"
    elif outcome.credential_path is not None:
        credential = f"no new sync credential; keeping {outcome.credential_path}"
    else:
        credential = (
            "WARNING: no sync credential on file; a hub in enforce mode will "
            "refuse this machine. Enroll again with a fresh grant."
        )
    return [
        f"{verb} {outcome.name} ({outcome.machine_id}) on hub "
        f"{outcome.hub_machine_id} as {outcome.owner_email} "
        f"via {outcome.enrolled_via} at {outcome.enrolled_at}",
        credential,
    ]


def grant_lines(minted: enrollment_credentials.MintedGrant) -> list[str]:
    """The grant readout. The token is LAST so a copy-paste cannot miss it."""
    return [
        f"enrollment grant for {minted.owner.email}, single use, "
        f"expires {minted.expires_at}",
        "carry it to the joining machine and run:",
        "  python -m apps.sync_hub enroll --data-dir <its data dir> "
        "--hub <hub url> --name <its name> --grant-file -",
        "  (paste the token on stdin; --grant <token> works too but is "
        "readable in /proc and in shell history)",
        minted.token,
    ]


def fleet_lines(payload: dict[str, Any]) -> list[str]:
    """The fleet readout. The counts lead; the machines are their evidence."""
    lines = [
        f"hub: {payload['hub_machine_id']}",
        f"owned {payload['owned']}, unowned {payload['unowned']}, "
        f"foreign {payload['foreign']}",
    ]
    for machine in payload["machines"]:
        owner = machine["owner_email"] or "-"
        detail = f"  {machine['name']}  {machine['machine_id']}  {machine['state']}"
        if machine["state"] == "owned":
            detail += f"  {owner}  via {machine['enrolled_via']}"
        elif machine["state"] == "foreign":
            detail += (
                f"  claimed by hub {machine['row_hub_machine_id']}, which is "
                f"not this hub; re-enroll it to fix"
            )
        lines.append(detail)
    return lines


__all__ = [
    "ENROLL_PATH",
    "EnrollOutcome",
    "enroll",
    "enroll_lines",
    "fleet",
    "fleet_lines",
    "grant",
    "grant_lines",
    "local_machine_row",
    "read_grant_token",
]
