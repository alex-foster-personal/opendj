"""Turning a credential into an owner, for :mod:`apps.sync_hub.enrollment`.

Contract: ``specs/design_decision_12.md`` section B. This is the layer that
authenticates; :func:`apps.sync_hub.enrollment.enroll_machine` is the layer
that writes, and it never sees a credential. Two kinds resolve to the same
:class:`~apps.sync_hub.enrollment.OwnerIdentity`, which is what lets the dev
path and the user path share one writer:

* ``grant`` -- a short-lived single-use token minted on the hub by an
  operator who is already authenticated there, and carried once to the
  machine that is joining. This is the DEV path, and it is what a headless
  host like nucbox-wsl needs: there is no browser on it, and the webui's
  loopback OAuth redirect would come back to the wrong machine.
* ``google_id_token`` -- the USER path's slot. An install that already holds
  a Google refresh token mints a fresh id_token and presents it; Google is
  the third party both machines trust, so nothing is copied by hand. NOT
  IMPLEMENTED YET, and it says so with its own error type rather than
  answering like a bad credential -- see :class:`EnrollmentCredentialUnavailable`.

Why the existing session bearer is NOT a third kind, since it is the obvious
idea: :class:`apps.webui.server.auth.SessionStore` is sqlite persistence over
the LOCAL install's state DB, and ``users`` / ``auth_sessions`` are not in the
sync set. A token minted on nucbox-wsl exists only in nucbox's database; the
hub has never seen its hash and ``resolve()`` returns None. The credential is
real, and it is meaningless one machine over. A cross-machine credential has
to be one a third party vouches for.

A static pre-shared hub secret was considered and rejected: it names no
owner, so it cannot satisfy the ownership requirement at all, and a leaked one
stays indistinguishable from legitimate use forever.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apps.shared.state import sync_stamp
from apps.sync_hub.enrollment import OwnerIdentity

GRANT_TABLE: str = "enrollment_grants"

#: Default grant lifetime. Long enough to paste into a second terminal, short
#: enough that a token left in shell history is not a standing key.
GRANT_TTL_S: int = 900

#: Every minted grant starts with this. Two reasons, and the first one is a
#: measured bug rather than a style preference: ``secrets.token_urlsafe``
#: draws from the base64url alphabet, so about one token in sixty-four begins
#: with ``-``, and argparse then reads ``--grant -Xyz...`` as a missing value
#: and dies with "expected one argument". That surfaced as a 2-in-40 test
#: flake here; on nucbox-wsl it would have been an operator staring at an
#: argparse error that had nothing to do with enrollment. A leading letter
#: makes the value safe as a CLI argument, an env var and a URL component by
#: construction, rather than safe most of the time. Second, a distinctive
#: prefix makes a leaked grant greppable, which is why provider tokens
#: (``ghp_``, ``sk-``) carry one.
GRANT_TOKEN_PREFIX: str = "odjenr_"

GRANT_KIND: str = "grant"
GOOGLE_ID_TOKEN_KIND: str = "google_id_token"

#: Every credential kind the wire accepts. ``google_id_token`` is listed
#: because the slot is real and reserved, not because it resolves yet.
CREDENTIAL_KINDS: tuple[str, ...] = (GRANT_KIND, GOOGLE_ID_TOKEN_KIND)

#: Credential kind -> the ``machine_owners.enrolled_via`` it records.
ENROLLED_VIA_BY_KIND: dict[str, str] = {
    GRANT_KIND: "grant",
    GOOGLE_ID_TOKEN_KIND: "google_id_token",
}


class EnrollmentCredentialError(RuntimeError):
    """The credential did not establish an owner. The caller answers 401."""


class EnrollmentCredentialUnavailable(EnrollmentCredentialError):
    """The kind is real and reserved, but this build cannot resolve it.

    Distinct from a rejected credential on purpose. Answering 401 for
    "not built yet" would make the USER path's seam indistinguishable from a
    bad token, and whoever picks that work up would have nothing to find.
    """


@dataclass(frozen=True)
class GrantCredential:
    """A single-use enrollment grant, as presented by the joining machine."""

    value: str


@dataclass(frozen=True)
class GoogleIdTokenCredential:
    """A Google-issued id_token, as the USER path will present it."""

    value: str


EnrollmentCredential = GrantCredential | GoogleIdTokenCredential


@dataclass(frozen=True)
class MintedGrant:
    """A freshly minted grant. ``token`` is the ONLY time the raw value exists."""

    token: str
    owner: OwnerIdentity
    created_at: str
    expires_at: str


def hash_grant_token(token: str) -> str:
    """sha256 of a grant; only the hash reaches the database.

    Same shape and same reasoning as
    :func:`apps.webui.server.auth.hash_session_token`: a stolen database must
    not hand anybody a redeemable credential.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def credential_from_wire(kind: str, value: str) -> EnrollmentCredential:
    """Build the credential a payload describes, or refuse the kind."""
    if kind == GRANT_KIND:
        return GrantCredential(value=value)
    if kind == GOOGLE_ID_TOKEN_KIND:
        return GoogleIdTokenCredential(value=value)
    raise EnrollmentCredentialError(
        f"unknown enrollment credential kind {kind!r}; this hub accepts "
        f"{list(CREDENTIAL_KINDS)}."
    )


def owner_by_email(conn: sqlite3.Connection, email: str) -> OwnerIdentity:
    """The signed-in user with this email, or a loud failure.

    No auto-creation. A ``users`` row is the record of a real Google sign-in
    on this hub, and inventing one to make a mint succeed would put a
    fabricated owner on a real machine.
    """
    row = conn.execute(
        "SELECT google_sub, email FROM users WHERE email = ?", (email,)
    ).fetchone()
    if row is None:
        raise EnrollmentCredentialError(
            f"no signed-in user with email {email!r} on this hub. Ownership "
            f"is a Google identity, so somebody has to sign in through the "
            f"webui first; this command will not invent a user to enroll "
            f"a machine to."
        )
    return OwnerIdentity(google_sub=str(row[0]), email=str(row[1]))


def mint_grant(
    conn: sqlite3.Connection,
    *,
    owner: OwnerIdentity,
    ttl_s: int = GRANT_TTL_S,
    now: str | None = None,
) -> MintedGrant:
    """Mint one single-use grant for ``owner``. Returns the raw token once."""
    created = sync_stamp.parse_canonical(now) if now else datetime.now(UTC)
    token = GRANT_TOKEN_PREFIX + secrets.token_urlsafe(32)
    created_at = sync_stamp.canonical_from(created)
    expires_at = sync_stamp.canonical_from(created + timedelta(seconds=ttl_s))
    conn.execute(
        f"INSERT INTO {GRANT_TABLE}(grant_token_sha256, google_sub, "
        f"created_at, expires_at) VALUES (?, ?, ?, ?)",
        (hash_grant_token(token), owner.google_sub, created_at, expires_at),
    )
    return MintedGrant(
        token=token, owner=owner, created_at=created_at, expires_at=expires_at
    )


def resolve_enrollment_identity(
    conn: sqlite3.Connection,
    credential: EnrollmentCredential,
    *,
    machine_id: str,
    now: str | None = None,
) -> OwnerIdentity:
    """The owner ``credential`` proves, for ``machine_id``. Never a default.

    Exhaustive over the credential union: an unhandled member is an
    :class:`AssertionError` on a ``never``-typed binding, not a fall-through
    that could accept something unproven.
    """
    if isinstance(credential, GrantCredential):
        return _redeem_grant(conn, credential, machine_id=machine_id, now=now)
    if isinstance(credential, GoogleIdTokenCredential):
        raise EnrollmentCredentialUnavailable(
            "the google_id_token credential kind is the zero-ceremony USER "
            "path and is not implemented in this build. It needs a real JWKS "
            "verifier (signature by kid, iss, aud pinned to our OAuth client "
            "id, exp and iat) -- apps.webui.server.auth.decode_id_token_claims "
            "does NOT verify signatures and must not be reused here, because "
            "a token arriving from a spoke is exactly the untrusted hop its "
            "docstring excludes. Until then, enroll with a grant: "
            "python -m apps.sync_hub grant --data-dir <hub data dir>"
        )
    _exhaustive: object = credential
    raise AssertionError(f"unhandled enrollment credential {_exhaustive!r}")


def _redeem_grant(
    conn: sqlite3.Connection,
    credential: GrantCredential,
    *,
    machine_id: str,
    now: str | None = None,
) -> OwnerIdentity:
    """Spend one grant for ``machine_id``, or refuse and write nothing.

    Single use is scoped to one MACHINE, not to one call. A grant already
    redeemed by a DIFFERENT machine is refused; re-presenting it for the
    machine that already spent it, WITHIN ITS LIFETIME, resolves to the same
    owner, which is what makes ``enroll`` safely re-runnable.

    Expiry is checked before the redeemed branch and therefore applies to
    both. It used to apply only to an unspent grant, on the reasoning that
    re-presentation grants no new capability. That reasoning was wrong twice
    over: the call still writes the machine's registry row, and after a
    revocation a re-assertion would be a new capability rather than a
    repeated one. A spent grant that never expires is a standing key, which
    is the exact property ``GRANT_TTL_S`` exists to deny -- and the dev path
    puts that token in shell history on the joining machine, so "spent" is
    the state it spends its life in.
    """
    token_hash = hash_grant_token(credential.value)
    row = conn.execute(
        f"SELECT google_sub, expires_at, redeemed_at, redeemed_machine_id "
        f"FROM {GRANT_TABLE} WHERE grant_token_sha256 = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        raise EnrollmentCredentialError(
            "this enrollment grant is not known to this hub. Mint one on the "
            "hub with `python -m apps.sync_hub grant --data-dir <hub data "
            "dir> --owner <email>` and pass its token to --grant."
        )
    google_sub, expires_at, redeemed_at, redeemed_machine_id = row
    stamp = now or sync_stamp.canonical_now()

    if sync_stamp.parse_canonical(str(expires_at)) <= sync_stamp.parse_canonical(stamp):
        raise EnrollmentCredentialError(
            f"this enrollment grant expired at {expires_at}. Mint a fresh "
            f"one on the hub; the short life is the point. Expiry applies to "
            f"a grant that has already been redeemed too, so re-running "
            f"enroll long afterwards needs a new grant rather than the old "
            f"token."
        )
    if redeemed_at is not None:
        if str(redeemed_machine_id) != machine_id:
            raise EnrollmentCredentialError(
                f"this enrollment grant was already redeemed by machine "
                f"{redeemed_machine_id}. Grants are single use; mint a fresh "
                f"one for {machine_id}."
            )
    else:
        conn.execute(
            f"UPDATE {GRANT_TABLE} SET redeemed_at = ?, redeemed_machine_id = ? "
            f"WHERE grant_token_sha256 = ?",
            (stamp, machine_id, token_hash),
        )

    user = conn.execute(
        "SELECT google_sub, email FROM users WHERE google_sub = ?",
        (str(google_sub),),
    ).fetchone()
    if user is None:
        raise EnrollmentCredentialError(
            f"the grant names user {google_sub}, who no longer exists on this "
            f"hub; the account was deleted. Nothing can be enrolled to them."
        )
    return OwnerIdentity(google_sub=str(user[0]), email=str(user[1]))


__all__ = [
    "CREDENTIAL_KINDS",
    "ENROLLED_VIA_BY_KIND",
    "GOOGLE_ID_TOKEN_KIND",
    "GRANT_KIND",
    "GRANT_TABLE",
    "GRANT_TOKEN_PREFIX",
    "GRANT_TTL_S",
    "EnrollmentCredential",
    "EnrollmentCredentialError",
    "EnrollmentCredentialUnavailable",
    "GoogleIdTokenCredential",
    "GrantCredential",
    "MintedGrant",
    "credential_from_wire",
    "hash_grant_token",
    "mint_grant",
    "owner_by_email",
    "resolve_enrollment_identity",
]
