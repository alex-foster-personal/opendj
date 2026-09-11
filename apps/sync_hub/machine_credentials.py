"""Per-machine sync credentials on the hub: mint, verify, and the mode switch.

Plan X5 (``.planning/cloudsync-levels-map-2026-09-11.md``), an amendment to
ADR 12. After v9 a machine is identified on ``push``, ``pull``, ``status`` and
``digest`` by its ``machine_id`` alone, and the hub returns every
``machine_id`` it knows to any ``hello`` caller, so a machine id is not a
secret. Enforcing ownership by id alone would be spoofable by anybody who had
ever said hello. The credential minted here is the secret the id is not.

The lifecycle, end to end:

1. ``POST /api/v1/sync/enroll`` succeeds and :func:`mint_credential` stores
   the sha256 of a fresh ``odjsync_`` token in ``machine_credentials``
   (migration v11). The raw token rides that one response and is never
   stored or returned again.
2. The spoke writes it to ``<data-dir>/sync-credential`` at 0600
   (:mod:`apps.sync_hub.spoke_credential`) and sends it as
   ``Authorization: Bearer`` on hello, push, pull, status and digest.
3. Every one of those endpoints asks :func:`verify` for a
   :data:`CredentialVerdict`. What happens next is the MODE:

   * ``observe`` (the default) logs anything but ``valid`` and reports the
     verdict in the ``hello`` response. Nothing is refused, so every machine
     that synced yesterday syncs today.
   * ``enforce`` refuses anything but ``valid`` with 401.

The mode is configuration, read from ``MDT_SYNC_CREDENTIAL_MODE`` on every
request rather than cached, so an operator flips it by restarting nothing.
ENFORCE also refuses to ACTIVATE while :func:`enforce_blockers` names any
machine: switching it on over an unowned or uncredentialed machine would lock
that machine out, and a hub that silently stayed in observe instead would be
the false sense of authentication ``.claude/rules/verification.md`` is about.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.shared.state import machine_identity, sync_stamp

CREDENTIAL_TABLE: str = "machine_credentials"

#: Every minted credential starts with this, for the two reasons
#: :data:`apps.sync_hub.enrollment_credentials.GRANT_TOKEN_PREFIX` gives: a
#: leading letter keeps the value safe as a CLI argument, and a distinctive
#: prefix makes a leaked credential greppable.
CREDENTIAL_PREFIX: str = "odjsync_"

MODE_ENV: str = "MDT_SYNC_CREDENTIAL_MODE"

CredentialMode = Literal["observe", "enforce"]
MODES: tuple[CredentialMode, ...] = ("observe", "enforce")

#: Named rather than implied: an unset ``MDT_SYNC_CREDENTIAL_MODE`` means
#: exactly this, and it is observe so that shipping the credential changes
#: nothing for a fleet that has not enrolled yet.
DEFAULT_MODE: CredentialMode = "observe"

#: How the hub reads one call's credential.
#:
#: * ``valid``   -- the bearer hashes to this machine's row and the machine is
#:   owned on this hub.
#: * ``missing`` -- no ``Authorization`` header at all.
#: * ``invalid`` -- a header that is not ``Bearer <token>``, or a token that is
#:   not this machine's (including a machine that holds no credential).
#: * ``revoked`` -- the machine's owner row is revoked; nothing it sends is
#:   valid.
#: * ``unowned`` -- the token matches, but this hub does not read the machine
#:   as owned (a hub restored from another machine's backup, where every
#:   owner row reads FOREIGN).
CredentialVerdict = Literal["valid", "missing", "invalid", "revoked", "unowned"]

#: Why a machine stops ENFORCE activating. ``no_credential`` is stricter than
#: ADR 12's ``unowned == 0`` gate on purpose: an owned machine that holds no
#: credential (every machine enrolled before v11) would be locked out the
#: moment ENFORCE activated, which is the outage the gate exists to prevent.
BlockerReason = Literal["unowned", "foreign", "no_credential"]


class CredentialModeError(ValueError):
    """``MDT_SYNC_CREDENTIAL_MODE`` holds something that is not a mode."""


@dataclass(frozen=True)
class EnforceBlocker:
    """One machine that stops ENFORCE from activating, and why."""

    machine_id: str
    name: str
    reason: BlockerReason

    def to_wire(self) -> dict[str, str]:
        return {"machine_id": self.machine_id, "name": self.name, "reason": self.reason}


# ----- mode ------------------------------------------------------------------


def configured_mode(env: Mapping[str, str] | None = None) -> CredentialMode:
    """The mode ``MDT_SYNC_CREDENTIAL_MODE`` selects. Unset means observe.

    Anything else that is not ``observe`` or ``enforce`` (case-insensitive)
    raises: ``MDT_SYNC_CREDENTIAL_MODE=on`` silently reading as observe would
    leave an operator believing the hub was enforcing.
    """
    raw = (os.environ if env is None else env).get(MODE_ENV, "").strip().lower()
    if raw == "":
        return DEFAULT_MODE
    if raw == "observe":
        return "observe"
    if raw == "enforce":
        return "enforce"
    raise CredentialModeError(
        f"{MODE_ENV}={raw!r} is not a credential mode; use one of "
        f"{list(MODES)} (unset means {DEFAULT_MODE!r})."
    )


def hub_machine_id_if_known(data_dir: Path) -> str | None:
    """This hub's id from its id file, or None when it has never minted one.

    Never creates the file: a refused call must not have a write side effect
    (round 3 finding R8), and a hub with no identity owns nothing anyway.
    """
    if not machine_identity.machine_id_path(Path(data_dir)).exists():
        return None
    return machine_identity.get_or_create_machine_id(Path(data_dir))


# ----- the store -------------------------------------------------------------


def hash_credential(token: str) -> str:
    """sha256 of a credential; only the hash reaches the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_credential(conn: sqlite3.Connection, machine_id: str, *, now: str | None = None) -> str:
    """Mint ``machine_id``'s credential, replacing any previous one.

    Returns the raw token, which exists only in this return value. The caller
    owns the transaction.
    """
    token = CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
    stamp = (
        sync_stamp.canonical_from(sync_stamp.parse_canonical(now))
        if now
        else sync_stamp.canonical_now()
    )
    conn.execute(
        f"INSERT INTO {CREDENTIAL_TABLE}(machine_id, credential_sha256, minted_at) "
        f"VALUES (?, ?, ?) ON CONFLICT(machine_id) DO UPDATE SET "
        f"credential_sha256 = excluded.credential_sha256, "
        f"minted_at = excluded.minted_at",
        (machine_id, hash_credential(token), stamp),
    )
    return token


def minted_at(conn: sqlite3.Connection, machine_id: str) -> str | None:
    """When ``machine_id``'s live credential was minted, or None if it has none."""
    row = conn.execute(
        f"SELECT minted_at FROM {CREDENTIAL_TABLE} WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def delete_credential(conn: sqlite3.Connection, machine_id: str) -> bool:
    """Drop ``machine_id``'s credential. True when one existed.

    The table is spelled literally, not through :data:`CREDENTIAL_TABLE`, so
    the hard-DELETE guard in ``tests/cloudsync/test_soft_delete.py`` can SEE
    that the target is outside the sync set rather than having to assume a
    dynamic name might be a synced table.
    """
    cursor = conn.execute("DELETE FROM machine_credentials WHERE machine_id = ?", (machine_id,))
    return cursor.rowcount > 0


def _bearer(authorization: str) -> str | None:
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def verify(
    conn: sqlite3.Connection,
    machine_id: str,
    authorization: str | None,
    *,
    hub_machine_id: str | None,
) -> CredentialVerdict:
    """Read one call's ``Authorization`` header against ``machine_id``.

    Revocation is checked FIRST, before the header is even parsed, so a
    revoked machine reads ``revoked`` whatever it sends. The hash compare is
    constant-time.
    """
    owner = conn.execute(
        "SELECT hub_machine_id, revoked_at FROM machine_owners WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()
    if owner is not None and owner[1] is not None:
        return "revoked"
    if authorization is None or not authorization.strip():
        return "missing"
    token = _bearer(authorization)
    stored = conn.execute(
        f"SELECT credential_sha256 FROM {CREDENTIAL_TABLE} WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()
    if token is None or stored is None:
        return "invalid"
    if not hmac.compare_digest(str(stored[0]), hash_credential(token)):
        return "invalid"
    if owner is None or hub_machine_id is None or str(owner[0]) != hub_machine_id:
        return "unowned"
    return "valid"


def enforce_blockers(
    conn: sqlite3.Connection, *, hub_machine_id: str | None
) -> list[EnforceBlocker]:
    """Every machine that would be locked out if ENFORCE activated now.

    Skips the hub's own row (it never calls itself) and revoked machines
    (locking those out is the point of revoking them). Re-derived on every
    call, never cached: a recorded "zero blockers" goes stale the moment a
    peer snapshot teaches the hub a new machine.
    """
    rows = conn.execute(
        f"""
        SELECT m.machine_id, m.name, o.hub_machine_id, o.revoked_at,
               c.machine_id IS NOT NULL
        FROM machines AS m
        LEFT JOIN machine_owners AS o ON o.machine_id = m.machine_id
        LEFT JOIN {CREDENTIAL_TABLE} AS c ON c.machine_id = m.machine_id
        WHERE m.machine_id != ?
        ORDER BY m.name
        """,
        (hub_machine_id or "",),
    ).fetchall()
    blockers: list[EnforceBlocker] = []
    for machine_id, name, owner_hub, revoked_at, has_credential in rows:
        reason: BlockerReason | None
        if revoked_at is not None:
            reason = None
        elif owner_hub is None:
            reason = "unowned"
        elif owner_hub != hub_machine_id:
            reason = "foreign"
        elif not has_credential:
            reason = "no_credential"
        else:
            reason = None
        if reason is not None:
            blockers.append(EnforceBlocker(str(machine_id), str(name), reason))
    return blockers


__all__ = [
    "CREDENTIAL_PREFIX",
    "CREDENTIAL_TABLE",
    "DEFAULT_MODE",
    "MODES",
    "MODE_ENV",
    "BlockerReason",
    "CredentialMode",
    "CredentialModeError",
    "CredentialVerdict",
    "EnforceBlocker",
    "configured_mode",
    "delete_credential",
    "enforce_blockers",
    "hash_credential",
    "hub_machine_id_if_known",
    "mint_credential",
    "minted_at",
    "verify",
]
