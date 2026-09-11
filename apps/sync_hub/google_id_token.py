"""Verifying a Google id_token that arrived from ANOTHER machine (ADR 12 B).

The ``google_id_token`` enrollment credential is the USER path: an install
that holds a Google refresh token mints a fresh id_token and presents it to
the hub, and Google is the third party both machines trust. This module is
the first place in the tree that verifies a signature, and it is the ONLY
thing standing between that token and an owner row.

Why :func:`apps.webui.server.auth.decode_id_token_claims` is not reused: it
reads claims without a signature check, which is safe only for a token that
came straight back from Google's token endpoint over TLS. A token arriving at
the hub from a spoke is exactly the untrusted hop its docstring excludes, and
anybody can base64 a JSON object that says ``"sub": "<the maintainer>"``.

What a token must prove, and each check fails closed:

* ``alg`` is ``RS256``. Checked before any key is looked up, so ``none`` and
  the HMAC key-confusion family never reach the verifier.
* the signature verifies against the key Google publishes for its ``kid``,
  fetched from :data:`GOOGLE_JWKS_URL` and cached for as long as Google's
  ``Cache-Control: max-age`` allows. A ``kid`` the fresh cache does not hold
  earns exactly one refetch, which is how a Google key rotation is picked up
  without waiting out the TTL.
* ``iss`` is one of :data:`GOOGLE_ISSUERS`, ``aud`` is THIS install's OAuth
  client id (from config, never a literal here), ``exp`` has not passed and
  ``iat`` is not in the future, each within :data:`CLOCK_SKEW_S`.
* ``email_verified`` is literally ``true``. An unverified address is a
  string the account holder typed, and owners are matched by it.

A hub that cannot verify (no client id configured, the key source
unreachable or answering garbage) raises
:class:`GoogleIdTokenVerifierUnavailable`. That is never a reason to accept:
there is no fallback to unverified claims anywhere in this module.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from apps.shared.google_oauth_client import CLIENT_ID_ENV_NAMES, first_present
from apps.shared.state import sync_stamp
from apps.sync_hub.enrollment import EnrollmentError, OwnerIdentity

# ----- constants -----------------------------------------------------------

#: Google's published id_token signing keys, as JWKS. The production value;
#: :data:`JWKS_URL_ENV_NAME` overrides it for a test or staging key source.
GOOGLE_JWKS_URL: str = "https://www.googleapis.com/oauth2/v3/certs"

#: Env var that points the verifier at a different JWKS. Only an ``https``
#: URL, or ``http`` on a loopback host, is accepted -- see
#: :func:`_require_trusted_jwks_url`.
JWKS_URL_ENV_NAME: str = "OPENDJ_GOOGLE_JWKS_URL"

#: Both spellings Google documents for ``iss`` on an id_token.
GOOGLE_ISSUERS: tuple[str, ...] = ("accounts.google.com", "https://accounts.google.com")

#: The only signing algorithm accepted. Google signs id_tokens with RS256.
ACCEPTED_ALGORITHM: str = "RS256"

#: Seconds of clock disagreement tolerated on ``exp`` and ``iat``. Small on
#: purpose: it absorbs NTP drift between hub, spoke and Google, and nothing
#: more. Google id_tokens live for an hour, so a minute of grace changes
#: nothing about how long a leaked one stays useful.
CLOCK_SKEW_S: int = 60

#: A JWKS fetch that has not answered in this long fails the enrollment.
JWKS_FETCH_TIMEOUT_S: float = 5.0

#: Claims a token must carry at all. Their VALUES are checked separately.
REQUIRED_CLAIMS: tuple[str, ...] = ("exp", "iat", "iss", "aud", "sub")

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_AGE = re.compile(r"^max-age\s*=\s*(\d+)$", re.IGNORECASE)

_MISSING_CLIENT_ID_RUNBOOK = (
    "this hub cannot verify Google id_tokens: no OAuth client id is "
    "configured, so there is no audience to pin. Set one of {names} in the "
    "hub's environment (Doppler: project general, config dev_personal) to "
    "the SAME client id the installs sign in with. Until then, enroll with a "
    "grant: python -m apps.sync_hub grant --data-dir <hub data dir>"
)


class GoogleIdTokenRejected(RuntimeError):
    """The token does not prove a Google identity. The caller answers 401."""


class GoogleIdTokenVerifierUnavailable(RuntimeError):
    """This hub cannot verify right now. The caller answers 503, never 200.

    Distinct from :class:`GoogleIdTokenRejected` because the remedy is on the
    hub (configure a client id, restore egress to Google), not in the token.
    """


# ----- config --------------------------------------------------------------


@dataclass(frozen=True)
class GoogleIdTokenConfig:
    """What the verifier pins: the audience, and where the keys come from."""

    client_id: str
    jwks_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GoogleIdTokenConfig:
        """Build from ``env``; a missing client id is a loud 503, not a guess."""
        client_id = first_present(env, CLIENT_ID_ENV_NAMES)
        if client_id is None:
            raise GoogleIdTokenVerifierUnavailable(
                _MISSING_CLIENT_ID_RUNBOOK.format(names=" or ".join(CLIENT_ID_ENV_NAMES))
            )
        jwks_url = env.get(JWKS_URL_ENV_NAME, "").strip() or GOOGLE_JWKS_URL
        _require_trusted_jwks_url(jwks_url)
        return cls(client_id=client_id, jwks_url=jwks_url)


def _require_trusted_jwks_url(url: str) -> None:
    """Refuse a key source an on-path attacker could answer for.

    The keys decide who can mint an owner, so fetching them over plaintext
    from a remote host would hand that decision to the network. Loopback
    ``http`` is allowed because it never leaves the machine.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS:
        return
    raise GoogleIdTokenVerifierUnavailable(
        f"{JWKS_URL_ENV_NAME}={url!r} is not an https URL or a loopback http "
        f"URL. The signing keys decide who can become an owner, so they are "
        f"never fetched over a network an attacker could answer for."
    )


# ----- the JWKS cache ------------------------------------------------------


@dataclass(frozen=True)
class _KeySet:
    keys: dict[str, Any]
    expires_at: float


class JwksCache:
    """Google's signing keys for one JWKS URL, cached per ``Cache-Control``.

    ``clock`` is monotonic seconds; tests pass their own to step past a TTL
    without sleeping. The lock is held across a fetch on purpose: two
    enrollments racing on a cold cache make one request, not two.
    """

    def __init__(self, url: str, *, clock: Callable[[], float]) -> None:
        self.url = url
        self._clock = clock
        self._lock = threading.Lock()
        self._keyset: _KeySet | None = None

    def signing_key(self, kid: str) -> Any:
        """The public key for ``kid``. Fetches when stale, refetches ONCE on a miss."""
        with self._lock:
            fetched_now = False
            if self._keyset is None or self._clock() >= self._keyset.expires_at:
                self._keyset = self._fetch()
                fetched_now = True
            key = self._keyset.keys.get(kid)
            if key is None and not fetched_now:
                # The one refetch an unknown kid earns: Google rotated keys
                # inside our TTL. A second miss after it is a forged kid.
                self._keyset = self._fetch()
                key = self._keyset.keys.get(kid)
            if key is None:
                raise GoogleIdTokenRejected(
                    f"id_token kid {kid!r} is not among the signing keys "
                    f"published at {self.url}, even after a fresh fetch."
                )
            return key

    def _fetch(self) -> _KeySet:
        request = urllib.request.Request(self.url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=JWKS_FETCH_TIMEOUT_S) as response:
                raw = response.read()
                cache_control = response.headers.get("Cache-Control")
                age = response.headers.get("Age")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GoogleIdTokenVerifierUnavailable(
                f"could not fetch the id_token signing keys from {self.url}: "
                f"{exc}. The hub verifies every id_token against them and "
                f"will not accept one unverified."
            ) from exc
        keys = _signing_keys_from_jwks(raw, url=self.url)
        return _KeySet(keys=keys, expires_at=self._clock() + jwks_ttl_s(cache_control, age))


def _signing_keys_from_jwks(raw: bytes, *, url: str) -> dict[str, Any]:
    """``kid -> public key`` for every RS256 signing key in a JWKS body."""
    try:
        body = json.loads(raw)
        entries = body["keys"]
        keys = {
            str(entry["kid"]): jwt.PyJWK(entry, algorithm=ACCEPTED_ALGORITHM).key
            for entry in entries
            if entry.get("kty") == "RSA"
            and entry.get("use", "sig") == "sig"
            and entry.get("alg", ACCEPTED_ALGORITHM) == ACCEPTED_ALGORITHM
            and entry.get("kid")
        }
    except (ValueError, KeyError, TypeError, AttributeError, jwt.PyJWTError) as exc:
        raise GoogleIdTokenVerifierUnavailable(
            f"the JWKS at {url} is not a usable key set: {exc!r}"
        ) from exc
    if not keys:
        raise GoogleIdTokenVerifierUnavailable(f"the JWKS at {url} published no RS256 signing key.")
    return keys


def jwks_ttl_s(cache_control: str | None, age: str | None) -> float:
    """Seconds a JWKS response may be reused, per RFC 9111 ``max-age`` less ``Age``.

    No header, ``no-store``, ``no-cache``, or an ``Age`` that is not a number
    all mean zero: the next verification refetches. Guessing a lifetime the
    publisher did not state is how a revoked key stays trusted.
    """
    if cache_control is None:
        return 0.0
    directives = [part.strip() for part in cache_control.split(",")]
    lowered = {directive.lower() for directive in directives}
    if "no-store" in lowered or "no-cache" in lowered:
        return 0.0
    max_age = next(
        (int(match.group(1)) for d in directives if (match := _MAX_AGE.match(d))),
        None,
    )
    if max_age is None:
        return 0.0
    if age is None:
        return float(max_age)
    if not age.strip().isdigit():
        return 0.0
    return float(max(0, max_age - int(age.strip())))


_CACHES: dict[str, JwksCache] = {}
_CACHES_LOCK = threading.Lock()


def jwks_cache_for(url: str) -> JwksCache:
    """The process-wide cache for ``url``. One per URL, so tests never share."""
    with _CACHES_LOCK:
        cache = _CACHES.get(url)
        if cache is None:
            cache = JwksCache(url, clock=time.monotonic)
            _CACHES[url] = cache
        return cache


# ----- verification --------------------------------------------------------


@dataclass(frozen=True)
class VerifiedGoogleIdentity:
    """Who a VERIFIED id_token names. Built only by :func:`verify_google_id_token`."""

    google_sub: str
    email: str
    name: str | None
    picture: str | None


def verify_google_id_token(
    token: str, *, config: GoogleIdTokenConfig, cache: JwksCache
) -> VerifiedGoogleIdentity:
    """The identity ``token`` proves, or :class:`GoogleIdTokenRejected`."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise GoogleIdTokenRejected(f"id_token is not a JWT: {exc}") from exc
    algorithm = header.get("alg")
    if algorithm != ACCEPTED_ALGORITHM:
        raise GoogleIdTokenRejected(
            f"id_token alg {algorithm!r} is not {ACCEPTED_ALGORITHM}. Google "
            f"signs id_tokens with RS256 only; 'none' and HMAC are refused "
            f"before any key is looked up."
        )
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise GoogleIdTokenRejected("id_token header carries no kid to verify against.")
    key = cache.signing_key(kid)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[ACCEPTED_ALGORITHM],
            audience=config.client_id,
            issuer=list(GOOGLE_ISSUERS),
            leeway=CLOCK_SKEW_S,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        raise GoogleIdTokenRejected(
            f"id_token failed verification: {type(exc).__name__}: {exc}"
        ) from exc
    if claims["aud"] != config.client_id:
        # PyJWT accepts a LIST aud that merely contains ours. Google issues a
        # single-string aud for an installed-app sign-in, and a token minted
        # for several audiences was minted for somebody else too, so only
        # the exact string is ours.
        raise GoogleIdTokenRejected(
            f"id_token aud must be exactly this hub's client id, got "
            f"{claims['aud']!r}; a multi-valued aud is refused."
        )
    return _identity_from_verified_claims(claims)


def _identity_from_verified_claims(claims: dict[str, Any]) -> VerifiedGoogleIdentity:
    sub = claims.get("sub")
    email = claims.get("email")
    if not isinstance(sub, str) or not sub:
        raise GoogleIdTokenRejected("verified id_token carries no usable 'sub'.")
    if not isinstance(email, str) or not email:
        raise GoogleIdTokenRejected(
            "verified id_token carries no 'email'; the install must request the 'email' scope."
        )
    if claims.get("email_verified") is not True:
        raise GoogleIdTokenRejected(
            f"Google has not verified {email!r} (email_verified is "
            f"{claims.get('email_verified')!r}); an unverified address is not "
            f"an identity an owner can be matched by."
        )
    name = claims.get("name")
    picture = claims.get("picture")
    return VerifiedGoogleIdentity(
        google_sub=sub,
        email=email,
        name=name if isinstance(name, str) else None,
        picture=picture if isinstance(picture, str) else None,
    )


# ----- from a verified token to an owner -----------------------------------


def owner_for_verified_identity(
    conn: sqlite3.Connection, identity: VerifiedGoogleIdentity
) -> OwnerIdentity:
    """Make an ALREADY VERIFIED Google account a ``users`` row here.

    Split from :func:`verify_google_id_token` on purpose: verification does
    network I/O (a JWKS fetch, up to :data:`JWKS_FETCH_TIMEOUT_S`, plus one
    refetch on an unknown kid), so it runs BEFORE the enroll route opens its
    write transaction. Only this sqlite step runs inside it, and an
    unauthenticated caller can never hold the hub's write lock across a
    fetch.

    Unlike a grant, the id_token path may CREATE the ``users`` row: a token
    Google signed for our client IS a real sign-in, which is the thing
    :func:`apps.sync_hub.enrollment_credentials.owner_by_email` refuses to
    invent. ``machine_owners.google_sub`` references ``users``, so the row
    has to exist before the enrollment writer can name it. Caller owns the
    transaction, so a refusal further down rolls this upsert back too.

    The email clash check is case-insensitive: ``MAINTAINER@example.com`` under a
    new sub is the same mailbox as ``maintainer@example.com``, and matching it
    case-sensitively would let a second users row claim it.
    """
    clash = conn.execute(
        "SELECT google_sub FROM users WHERE lower(email) = lower(?) AND google_sub <> ?",
        (identity.email, identity.google_sub),
    ).fetchone()
    if clash is not None:
        raise EnrollmentError(
            f"{identity.email!r} already belongs to a different Google account "
            f"({clash[0]}) on this hub. Ownership is keyed on the Google sub, "
            f"so this token cannot take that user's place; resolve the stale "
            f"users row on the hub first."
        )
    stamp = sync_stamp.canonical_now()
    conn.execute(
        "INSERT INTO users(google_sub, email, name, avatar_url, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(google_sub) DO UPDATE SET email = excluded.email, "
        "name = COALESCE(excluded.name, users.name), "
        "avatar_url = COALESCE(excluded.avatar_url, users.avatar_url), "
        "updated_at = excluded.updated_at",
        (identity.google_sub, identity.email, identity.name, identity.picture, stamp, stamp),
    )
    return OwnerIdentity(google_sub=identity.google_sub, email=identity.email)


__all__ = [
    "ACCEPTED_ALGORITHM",
    "CLOCK_SKEW_S",
    "GOOGLE_ISSUERS",
    "GOOGLE_JWKS_URL",
    "JWKS_URL_ENV_NAME",
    "GoogleIdTokenConfig",
    "GoogleIdTokenRejected",
    "GoogleIdTokenVerifierUnavailable",
    "JwksCache",
    "VerifiedGoogleIdentity",
    "jwks_cache_for",
    "jwks_ttl_s",
    "owner_for_verified_identity",
    "verify_google_id_token",
]
