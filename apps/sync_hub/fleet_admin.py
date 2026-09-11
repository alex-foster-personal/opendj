"""Hub-local fleet administration: adopt, revoke, and the credential readout.

    python -m apps.sync_hub adopt       --data-dir DIR --machine-id ID --owner EMAIL
    python -m apps.sync_hub revoke      --data-dir DIR --machine-id ID
    python -m apps.sync_hub credentials --data-dir DIR [--json]

Plan X5 and ADR 12 section A. These run on the HUB, against its own state
DB, because they are acts of hub authority, exactly like ``grant`` and
``fleet`` in :mod:`apps.sync_hub.maintenance_enroll`. Their HTTP twins live
in ``apps/webui/server/routes/cloudsync_fleet.py`` and call these same
functions, so the CLI and the HTTP path cannot drift.

* **adopt** records an owner for a machine that is ALREADY in ``machines``
  (it synced before ownership existed), with ``enrolled_via='adopt'`` so the
  audit trail says it was claimed retroactively. It goes through
  :func:`apps.sync_hub.enrollment.enroll_machine`, the one writer, and it
  mints NO sync credential: a hub-local operator has no safe channel to hand
  a long-lived bearer to a remote machine. The machine collects its
  credential by running ``enroll`` with a fresh grant, which mints one for
  any owned machine that holds none. No ``--all`` flag, per ADR 12.
* **revoke** writes ``revoked_at`` and deletes the machine's credential, in
  one transaction. Under ENFORCE a revoked machine is refused on every
  endpoint: 401 on hello, push, pull, status and digest, 409 on enroll.
* **credentials** is the ENFORCE readiness readout: the configured mode, the
  machines that would block activation and why, and each machine's
  ownership and credential state. Never the credential itself.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import enrollment, enrollment_credentials, machine_credentials, protocol
from apps.sync_hub.client_transport_ops import _local_machine_row, state_db_path
from apps.sync_hub.transport import SyncTransportError


class UnknownMachineError(enrollment.EnrollmentError):
    """The machine id is not in this hub's ``machines`` table."""


@dataclass(frozen=True)
class AdoptOutcome:
    machine_id: str
    name: str
    owner_email: str
    enrolled_at: str
    enrolled_via: str
    created: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "name": self.name,
            "owner_email": self.owner_email,
            "enrolled_at": self.enrolled_at,
            "enrolled_via": self.enrolled_via,
            "created": self.created,
        }


@dataclass(frozen=True)
class RevokeOutcome:
    machine_id: str
    revoked_at: str
    changed: bool
    credential_deleted: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "revoked_at": self.revoked_at,
            "changed": self.changed,
            "credential_deleted": self.credential_deleted,
        }


# ----- plumbing --------------------------------------------------------------


@contextmanager
def _hub_transaction(data_dir: Path) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(state_db_path(Path(data_dir)))
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _registered_machine(conn: sqlite3.Connection, machine_id: str) -> protocol.MachineRow:
    try:
        return _local_machine_row(conn, machine_id)
    except SyncTransportError as exc:
        raise UnknownMachineError(
            f"machine {machine_id} is not in this hub's machines table. adopt "
            f"claims a machine that already syncs; a new one joins with "
            f"`python -m apps.sync_hub enroll` instead."
        ) from exc


# ----- the operations (CLI and HTTP both call these) -------------------------


def adopt(data_dir: Path, *, machine_id: str, owner: enrollment.OwnerIdentity) -> AdoptOutcome:
    """Record ``owner`` for an already-registered machine. Mints no credential."""
    hub_machine_id = machine_identity.get_or_create_machine_id(Path(data_dir))
    with _hub_transaction(data_dir) as conn:
        machine = _registered_machine(conn, machine_id)
        result = enrollment.enroll_machine(
            conn,
            machine=machine,
            owner=owner,
            hub_machine_id=hub_machine_id,
            enrolled_via="adopt",
        )
    return AdoptOutcome(
        machine_id=result.machine_id,
        name=machine.name,
        owner_email=result.owner.email,
        enrolled_at=result.enrolled_at,
        enrolled_via=result.enrolled_via,
        created=result.created,
    )


def adopt_by_email(data_dir: Path, *, machine_id: str, owner_email: str) -> AdoptOutcome:
    """The CLI's adopt: the owner must already have signed in on this hub."""
    with _hub_transaction(data_dir) as conn:
        owner = enrollment_credentials.owner_by_email(conn, owner_email)
    return adopt(data_dir, machine_id=machine_id, owner=owner)


def revoke(
    data_dir: Path, *, machine_id: str, require_owner_sub: str | None = None
) -> RevokeOutcome:
    """Revoke ``machine_id`` and delete its credential, in one transaction."""
    with _hub_transaction(data_dir) as conn:
        result = enrollment.revoke_owner(conn, machine_id, require_owner_sub=require_owner_sub)
        deleted = machine_credentials.delete_credential(conn, machine_id)
    return RevokeOutcome(
        machine_id=result.machine_id,
        revoked_at=result.revoked_at,
        changed=result.changed,
        credential_deleted=deleted,
    )


def credentials(data_dir: Path) -> dict[str, Any]:
    """The ENFORCE readiness readout as one payload. Never carries a token."""
    mode = machine_credentials.configured_mode()
    hub_machine_id = machine_identity.get_or_create_machine_id(Path(data_dir))
    with _hub_transaction(data_dir) as conn:
        blockers = machine_credentials.enforce_blockers(conn, hub_machine_id=hub_machine_id)
        rows = conn.execute(
            """
            SELECT m.machine_id, m.name, o.hub_machine_id, o.revoked_at, c.minted_at
            FROM machines AS m
            LEFT JOIN machine_owners AS o ON o.machine_id = m.machine_id
            LEFT JOIN machine_credentials AS c ON c.machine_id = m.machine_id
            ORDER BY m.name
            """
        ).fetchall()
    machines = [
        {
            "machine_id": str(machine_id),
            "name": str(name),
            "ownership": _ownership_label(owner_hub, revoked_at, hub_machine_id),
            "credential_minted_at": None if minted is None else str(minted),
            "is_this_hub": str(machine_id) == hub_machine_id,
        }
        for machine_id, name, owner_hub, revoked_at, minted in rows
    ]
    return {
        "hub_machine_id": hub_machine_id,
        "mode": mode,
        "enforce_ready": not blockers,
        "blockers": [blocker.to_wire() for blocker in blockers],
        "machines": machines,
    }


def _ownership_label(owner_hub: object, revoked_at: object, hub_machine_id: str) -> str:
    if revoked_at is not None:
        return "revoked"
    if owner_hub is None:
        return "unowned"
    if owner_hub == hub_machine_id:
        return "owned"
    return "foreign"


# ----- CLI -------------------------------------------------------------------


def credentials_lines(payload: dict[str, Any]) -> list[str]:
    ready = "ready" if payload["enforce_ready"] else "NOT ready"
    lines = [
        f"hub: {payload['hub_machine_id']}",
        f"mode: {payload['mode']} ({machine_credentials.MODE_ENV}); enforce "
        f"{ready}, {len(payload['blockers'])} blocker(s)",
    ]
    lines.extend(
        f"  BLOCKER {blocker['name']}  {blocker['machine_id']}  {blocker['reason']}"
        for blocker in payload["blockers"]
    )
    for machine in payload["machines"]:
        minted = machine["credential_minted_at"] or "no credential"
        hub = "  (this hub)" if machine["is_this_hub"] else ""
        lines.append(
            f"  {machine['name']}  {machine['machine_id']}  {machine['ownership']}  {minted}{hub}"
        )
    return lines


def _print_adopt(args: argparse.Namespace) -> None:
    outcome = adopt_by_email(args.data_dir, machine_id=args.machine_id, owner_email=args.owner)
    verb = "adopted" if outcome.created else "already owned, nothing changed:"
    print(
        f"{verb} {outcome.name} ({outcome.machine_id}) for {outcome.owner_email} "
        f"via {outcome.enrolled_via} at {outcome.enrolled_at}"
    )
    print(
        "no sync credential was minted; run `python -m apps.sync_hub enroll` "
        "on that machine with a fresh grant to collect one"
    )


def _print_revoke(args: argparse.Namespace) -> None:
    outcome = revoke(args.data_dir, machine_id=args.machine_id)
    verb = "revoked" if outcome.changed else "already revoked, nothing changed:"
    dropped = "credential deleted" if outcome.credential_deleted else "no credential held"
    print(f"{verb} {outcome.machine_id} at {outcome.revoked_at}; {dropped}")


def _print_credentials(args: argparse.Namespace) -> None:
    payload = credentials(args.data_dir)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for line in credentials_lines(payload):
        print(line)


def add_subcommands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    """Register adopt, revoke and credentials on the ``apps.sync_hub`` CLI."""
    adopt_command = subcommands.add_parser(
        "adopt", parents=[common], help="record an owner for a machine that already syncs"
    )
    adopt_command.add_argument("--machine-id", required=True)
    adopt_command.add_argument(
        "--owner", required=True, help="email of a user who has signed in on this hub"
    )
    revoke_command = subcommands.add_parser(
        "revoke", parents=[common], help="revoke a machine and delete its sync credential"
    )
    revoke_command.add_argument("--machine-id", required=True)
    credentials_command = subcommands.add_parser(
        "credentials", parents=[common], help="print the ENFORCE readiness readout"
    )
    credentials_command.add_argument("--json", action="store_true")


PRINTING_COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "adopt": _print_adopt,
    "revoke": _print_revoke,
    "credentials": _print_credentials,
}


__all__ = [
    "PRINTING_COMMANDS",
    "AdoptOutcome",
    "RevokeOutcome",
    "UnknownMachineError",
    "add_subcommands",
    "adopt",
    "adopt_by_email",
    "credentials",
    "credentials_lines",
    "revoke",
]
