"""The one place a machine becomes owned. Contract: ``specs/design_decision_12.md``.

the maintainer's requirement has two paths and one mechanism:

  * the DEV path -- ``python -m apps.sync_hub grant`` on the hub, then
    ``python -m apps.sync_hub enroll`` on the machine that is joining;
  * the USER path -- an install joins its signed-in user's fleet with no CLI
    and no copied identifiers.

:func:`enroll_machine` is what both drive, and it is the only function in the
tree that writes ``machine_owners``. It takes an ALREADY RESOLVED owner: it
authenticates nothing, reads no credential, and cannot tell which path called
it except through ``enrolled_via``, which it records. Credential resolution
is the separate layer in :mod:`apps.sync_hub.enrollment_credentials`. That
split is what makes the two paths structurally incapable of diverging, rather
than merely discouraged from it -- the failure this repo has already measured
elsewhere, in four independent implementations of "create a playlist".

Three invariants:

1. **An owner row counts only on the hub that wrote it.** A hub restored from
   another machine's backup inherits this table but not its ``machine-id``
   file, which lives outside the DB on purpose (ADR 05 section 1). A row
   whose ``hub_machine_id`` is not this hub's live id is FOREIGN: reported
   loudly and never honored. Re-enrollment by THAT ROW'S OWNER is the
   resolution and it is one command; re-enrollment by anybody else is
   refused, because a restore must not be a way to take another user's
   machine. The re-enrollment overwrites the foreign row -- ``machine_id`` is
   the primary key, so there is one row per machine and no history is kept.
   This module is therefore not an audit log, and nothing here should be read
   as one.
2. **Enrolling an already-enrolled machine changes nothing.** Not "writes the
   same values again" -- literally no owner write, so ``enrolled_at`` stays
   the record of when the machine actually joined. That is what makes the dev
   path safely re-runnable, which is the difference between a command and a
   ritual.
3. **A revocation is never lifted as a side effect.** A revoked row is
   refused by the writer, loudly, rather than being cleared by the next
   enroll. :func:`revoke_owner` is the only thing that writes ``revoked_at``
   (driven by ``python -m apps.sync_hub revoke`` and its HTTP twin), and no
   command in this build un-revokes.

:func:`enroll_machine` and :func:`revoke_owner` are the only two writers of
``machine_owners``, and both live here. Ownership alone authenticates
nothing: the per-machine sync credential (:mod:`apps.sync_hub.machine_credentials`,
plan X5) is what the sync endpoints check, and only under
``MDT_SYNC_CREDENTIAL_MODE=enforce``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from apps.shared.state import sync_stamp
from apps.shared.state.migrations_v9 import ENROLLED_VIA_VALUES
from apps.sync_hub.engine_machines import merge_machines
from apps.sync_hub.protocol import MachineRow

OWNERSHIP_TABLE: str = "machine_owners"

#: How a machine reads against a given hub. ``foreign`` is not a variant of
#: ``unowned``: it means an owner row EXISTS and this hub refuses to believe
#: it, which is a different thing to report and a different thing to fix.
OwnershipState = Literal["owned", "unowned", "foreign"]


class EnrollmentError(RuntimeError):
    """A machine could not be enrolled. Never swallowed, never defaulted."""


class OwnershipConflictError(EnrollmentError):
    """The machine is already owned by a DIFFERENT user.

    Raised whether the standing row was written by this hub or by another one.
    A foreign row is not honored for READING ownership, but it is still
    somebody's claim, and re-enrolling over it under a second identity would
    be a transfer performed by whoever restored the hub.
    """


class MachineRevokedError(EnrollmentError):
    """The machine's owner row is revoked, and enroll will not lift it.

    Separate from :class:`OwnershipConflictError` because the remedy is
    different: a conflict is resolved by the rightful owner enrolling, and a
    revocation is resolved by a deliberate decision to un-revoke, which no
    command in this build makes.
    """


@dataclass(frozen=True)
class OwnerIdentity:
    """A resolved owner. Whoever built this has already proved the claim."""

    google_sub: str
    email: str


@dataclass(frozen=True)
class EnrollmentResult:
    """What one call to :func:`enroll_machine` left behind.

    ``created`` is False when the machine was already enrolled to this owner
    on this hub and the call wrote nothing. A caller that reported success
    either way would hide a duplicate from an operator, so this is part of
    the result rather than something the caller infers.
    """

    machine_id: str
    owner: OwnerIdentity
    hub_machine_id: str
    enrolled_at: str
    enrolled_via: str
    created: bool


@dataclass(frozen=True)
class _OwnerRow:
    """One ``machine_owners`` row exactly as stored, nothing filtered out."""

    google_sub: str
    hub_machine_id: str
    enrolled_at: str
    enrolled_via: str
    revoked_at: str | None


@dataclass(frozen=True)
class MachineOwnership:
    """One machine's ownership as this hub reads it right now."""

    machine_id: str
    name: str
    state: OwnershipState
    owner_google_sub: str | None
    owner_email: str | None
    row_hub_machine_id: str | None
    enrolled_at: str | None
    enrolled_via: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "name": self.name,
            "state": self.state,
            "owner_google_sub": self.owner_google_sub,
            "owner_email": self.owner_email,
            "row_hub_machine_id": self.row_hub_machine_id,
            "enrolled_at": self.enrolled_at,
            "enrolled_via": self.enrolled_via,
        }


# ----- the single writer ---------------------------------------------------


def enroll_machine(
    conn: sqlite3.Connection,
    *,
    machine: MachineRow,
    owner: OwnerIdentity,
    hub_machine_id: str,
    enrolled_via: str,
    now: str | None = None,
) -> EnrollmentResult:
    """Register ``machine`` and record ``owner`` as its owner on this hub.

    Registration and ownership are one act in one transaction: a machine that
    is owned but absent from ``machines`` would violate the foreign key, and a
    machine registered by an enrollment that then failed would be a fleet row
    nobody asked for.

    Idempotent when the owner row already names ``owner`` on THIS hub:
    nothing is written to ``machine_owners`` and ``created`` is False. A row
    naming a DIFFERENT user raises :class:`OwnershipConflictError`, and it is
    checked BEFORE the hub binding rather than inside it -- on a restored hub
    every row is foreign, and a guard that only fires for rows this hub wrote
    would be absent in exactly the situation it exists for. A revoked row
    raises :class:`MachineRevokedError` rather than being cleared.

    A foreign row belonging to ``owner`` IS replaced, because re-enrollment is
    the documented resolution for one; the prior hub id and stamp do not
    survive it (module docstring, invariant 1).

    The caller owns the transaction. Every raise here happens before anything
    is written, and the transaction rolls back regardless, so a refused
    enrollment leaves the hub exactly as it was -- two independent reasons,
    because relying on rollback alone would make the ordering load-bearing
    and silent.
    """
    if enrolled_via not in ENROLLED_VIA_VALUES:
        raise EnrollmentError(
            f"enrolled_via {enrolled_via!r} is not one of "
            f"{list(ENROLLED_VIA_VALUES)}; machine_owners CHECKs this column."
        )
    existing = _owner_row(conn, machine.machine_id)
    if existing is not None:
        _refuse_unless_re_enrollable(
            existing,
            machine_id=machine.machine_id,
            owner=owner,
            hub_machine_id=hub_machine_id,
        )
    if existing is not None and existing.hub_machine_id == hub_machine_id:
        # Return BEFORE the merge, not after. Sol review, PR #1648 (P2): the
        # upsert consumed the caller-supplied row, so a re-run with a
        # different --name quietly renamed the fleet row while the CLI
        # printed "already enrolled" and this result said created=False. A
        # no-op has to be a no-op. Refreshing a machine's registry fields is
        # what `hello` is for, on every handshake; enroll is about ownership.
        return EnrollmentResult(
            machine_id=machine.machine_id,
            owner=owner,
            hub_machine_id=hub_machine_id,
            enrolled_at=existing.enrolled_at,
            enrolled_via=existing.enrolled_via,
            created=False,
        )
    merge_machines(conn, [machine], caller_id=machine.machine_id)

    # Round-tripped, not trusted. Sol review, PR #1648 (P1): the public
    # writer took any non-empty string and put it straight into
    # machine_owners.enrolled_at, a column documented as canonical UTC, so a
    # caller could persist a naive or offset stamp into the ownership ledger
    # -- the same class of defect the sync set's naive-stamp quarantine
    # exists for. parse_canonical raises on anything it cannot order.
    stamp = (
        sync_stamp.canonical_from(sync_stamp.parse_canonical(now))
        if now
        else sync_stamp.canonical_now()
    )
    # No ``revoked_at`` in the UPDATE list, deliberately: this statement must
    # not be able to lift a revocation even if the guard above is ever
    # loosened. Reaching here with a revoked row is impossible today.
    conn.execute(
        f"""
        INSERT INTO {OWNERSHIP_TABLE}(
            machine_id, google_sub, hub_machine_id, enrolled_at,
            enrolled_via, revoked_at
        ) VALUES (?, ?, ?, ?, ?, NULL)
        ON CONFLICT(machine_id) DO UPDATE SET
            google_sub     = excluded.google_sub,
            hub_machine_id = excluded.hub_machine_id,
            enrolled_at    = excluded.enrolled_at,
            enrolled_via   = excluded.enrolled_via
        """,
        (
            machine.machine_id,
            owner.google_sub,
            hub_machine_id,
            stamp,
            enrolled_via,
        ),
    )
    return EnrollmentResult(
        machine_id=machine.machine_id,
        owner=owner,
        hub_machine_id=hub_machine_id,
        enrolled_at=stamp,
        enrolled_via=enrolled_via,
        created=True,
    )


@dataclass(frozen=True)
class RevocationResult:
    """What one :func:`revoke_owner` call left behind. ``changed`` is False
    when the row was already revoked and the call wrote nothing."""

    machine_id: str
    google_sub: str
    revoked_at: str
    changed: bool


def revoke_owner(
    conn: sqlite3.Connection,
    machine_id: str,
    *,
    require_owner_sub: str | None = None,
    now: str | None = None,
) -> RevocationResult:
    """Write ``revoked_at`` on ``machine_id``'s owner row. Caller owns the txn.

    Refuses a machine with NO owner row rather than inventing one to revoke:
    an unowned machine holds no sync credential, so ENFORCE already refuses
    it, and a revocation needs a claim to withdraw. ``require_owner_sub`` is
    the HTTP path's guard -- a signed-in user may revoke only machines they
    own -- and the hub-local CLI passes None. Idempotent: an already revoked
    row is reported with ``changed`` False and its ORIGINAL stamp.
    """
    existing = _owner_row(conn, machine_id)
    if existing is None:
        raise EnrollmentError(
            f"machine {machine_id} has no owner row on this hub, so there is "
            f"no claim to revoke. It holds no sync credential either, so a hub "
            f"in enforce mode already refuses it."
        )
    if require_owner_sub is not None and existing.google_sub != require_owner_sub:
        raise OwnershipConflictError(
            f"machine {machine_id} is owned by {existing.google_sub}, not by the "
            f"signed-in user; only its owner, or the hub-local CLI, can revoke it."
        )
    if existing.revoked_at is not None:
        return RevocationResult(machine_id, existing.google_sub, existing.revoked_at, False)
    stamp = (
        sync_stamp.canonical_from(sync_stamp.parse_canonical(now))
        if now
        else sync_stamp.canonical_now()
    )
    conn.execute(
        f"UPDATE {OWNERSHIP_TABLE} SET revoked_at = ? "
        f"WHERE machine_id = ? AND revoked_at IS NULL",
        (stamp, machine_id),
    )
    return RevocationResult(machine_id, existing.google_sub, stamp, True)


# ----- readers -------------------------------------------------------------


def _owner_row(conn: sqlite3.Connection, machine_id: str) -> _OwnerRow | None:
    """The standing owner row, whatever state it is in, or None.

    Filtered by NEITHER hub nor revocation, on purpose and in both cases for
    the same reason: a writer that cannot see a row cannot refuse to trample
    it. The revoked filter used to live here, which made a revoked row
    invisible to :func:`enroll_machine` and therefore silently overwritable.
    :func:`owner_for` is the filtered reader and the one anything outside this
    module should use to ask who owns a machine.
    """
    row = conn.execute(
        f"SELECT google_sub, hub_machine_id, enrolled_at, enrolled_via, "
        f"revoked_at FROM {OWNERSHIP_TABLE} WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()
    if row is None:
        return None
    return _OwnerRow(
        google_sub=str(row[0]),
        hub_machine_id=str(row[1]),
        enrolled_at=str(row[2]),
        enrolled_via=str(row[3]),
        revoked_at=None if row[4] is None else str(row[4]),
    )


def _refuse_unless_re_enrollable(
    existing: _OwnerRow, *, machine_id: str, owner: OwnerIdentity, hub_machine_id: str
) -> None:
    """Raise unless ``owner`` may enroll over ``existing``. Writes nothing."""
    if existing.revoked_at is not None:
        raise MachineRevokedError(
            f"machine {machine_id} has a REVOKED owner row (revoked at "
            f"{existing.revoked_at}, owner {existing.google_sub}). Enrolling "
            f"it would lift that revocation as a side effect, so it is "
            f"refused. Nothing in this build writes revoked_at, so whoever "
            f"set it did so deliberately; un-revoking has to be as deliberate."
        )
    if existing.google_sub == owner.google_sub:
        return
    if existing.hub_machine_id == hub_machine_id:
        whose = f"a row this hub wrote at {existing.enrolled_at}"
    else:
        whose = (
            f"a row written by hub {existing.hub_machine_id}, which is not "
            f"this hub. That row is FOREIGN and is not honored when reading "
            f"ownership, but it is still somebody's claim, and a hub restored "
            f"from a backup does not get to reassign their machines"
        )
    raise OwnershipConflictError(
        f"machine {machine_id} is already owned by {existing.google_sub} per "
        f"{whose}; enrolling it to {owner.google_sub} would be a transfer, "
        f"which must be an explicit act and not a side effect of re-running "
        f"enroll. There is no transfer command yet (ADR 12: adopt is "
        f"unbuilt), so this needs a deliberate hub-local decision."
    )


def owner_for(
    conn: sqlite3.Connection, machine_id: str, *, hub_machine_id: str
) -> OwnerIdentity | None:
    """The owner this hub believes, or None. Applies the hub-binding guard.

    None covers three genuinely different states -- never enrolled, revoked,
    and owned according to a row this hub did not write. Anything that has to
    tell them apart asks :func:`ownership_state`, which is why this returns an
    identity rather than a verdict.
    """
    row = conn.execute(
        f"SELECT o.google_sub, u.email FROM {OWNERSHIP_TABLE} AS o "
        f"JOIN users AS u ON u.google_sub = o.google_sub "
        f"WHERE o.machine_id = ? AND o.revoked_at IS NULL "
        f"AND o.hub_machine_id = ?",
        (machine_id, hub_machine_id),
    ).fetchone()
    if row is None:
        return None
    return OwnerIdentity(google_sub=str(row[0]), email=str(row[1]))


def ownership_state(
    conn: sqlite3.Connection, machine_id: str, *, hub_machine_id: str
) -> OwnershipState:
    """``owned`` / ``unowned`` / ``foreign`` for one machine on this hub.

    A revoked row reads ``unowned``: the machine is not owned, and the row is
    a record of a claim that was withdrawn rather than a competing one.
    """
    existing = _owner_row(conn, machine_id)
    if existing is None or existing.revoked_at is not None:
        return "unowned"
    return "owned" if existing.hub_machine_id == hub_machine_id else "foreign"


def fleet_ownership(
    conn: sqlite3.Connection, *, hub_machine_id: str
) -> list[MachineOwnership]:
    """Every known machine with the ownership this hub reads for it.

    Every machine, not only the owned ones: the readout has to be able to
    NAME the gap, or an operator closing it before the enforcement flip has
    nothing to work from.
    """
    cursor = conn.execute(
        f"""
        SELECT m.machine_id, m.name, o.google_sub, u.email,
               o.hub_machine_id, o.enrolled_at, o.enrolled_via
        FROM machines AS m
        LEFT JOIN {OWNERSHIP_TABLE} AS o
          ON o.machine_id = m.machine_id AND o.revoked_at IS NULL
        LEFT JOIN users AS u ON u.google_sub = o.google_sub
        ORDER BY m.name
        """
    )
    fleet: list[MachineOwnership] = []
    for row in cursor:
        row_hub = None if row[4] is None else str(row[4])
        if row_hub is None:
            state: OwnershipState = "unowned"
        elif row_hub == hub_machine_id:
            state = "owned"
        else:
            state = "foreign"
        fleet.append(
            MachineOwnership(
                machine_id=str(row[0]),
                name=str(row[1]),
                state=state,
                owner_google_sub=None if row[2] is None else str(row[2]),
                owner_email=None if row[3] is None else str(row[3]),
                row_hub_machine_id=row_hub,
                enrolled_at=None if row[5] is None else str(row[5]),
                enrolled_via=None if row[6] is None else str(row[6]),
            )
        )
    return fleet


def ownership_counts(fleet: list[MachineOwnership]) -> dict[str, int]:
    """``{owned, unowned, foreign}`` over ``fleet``, every key always present.

    All three keys are emitted even at zero. An absent key would read as "not
    measured", and a readout cannot tell a caller apart from a measurement
    that never ran if it omits the zeroes.
    """
    counts = {"owned": 0, "unowned": 0, "foreign": 0}
    for machine in fleet:
        counts[machine.state] += 1
    return counts


__all__ = [
    "OWNERSHIP_TABLE",
    "EnrollmentError",
    "EnrollmentResult",
    "MachineOwnership",
    "MachineRevokedError",
    "OwnerIdentity",
    "OwnershipConflictError",
    "OwnershipState",
    "RevocationResult",
    "enroll_machine",
    "fleet_ownership",
    "owner_for",
    "ownership_counts",
    "ownership_state",
    "revoke_owner",
]
