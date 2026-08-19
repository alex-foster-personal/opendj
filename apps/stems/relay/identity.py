"""Who is calling the relay, are they allowed, and have they used their share.

THREE GATES, IN ORDER, AND NONE OF THEM IS SKIPPABLE:

  1. VERIFY  the Google identity token is genuine, unexpired, and issued for
             OUR client id. A token that merely parses proves nothing -- the
             signature and the ``aud`` claim are what stop anyone with any
             Google account from spending the maintainer's GPU budget.
  2. ALLOW   the verified email is on the tester allowlist. Verification says
             who they are; the allowlist says whether we invited them.
  3. METER   the tester is inside their rate cap. A trusted tester with a
             runaway loop is the same invoice as an untrusted one.

THERE IS NO DEVELOPMENT BYPASS. No ``if DEBUG: return "maintainer@..."``, no
``ALLOW_ALL`` flag, no "skip verification when the issuer is localhost". Every
one of those is a switch that eventually ships, and the thing it disables is
the only thing standing between a public URL and an unbounded bill. Tests
inject a verifier OBJECT (see :class:`IdentityVerifier`); they never turn the
gate off.

ON HOW THE TOKEN IS VERIFIED. This calls Google's own ``tokeninfo`` endpoint
rather than verifying the RS256 signature locally against Google's JWKS.
Local verification is the better answer at scale -- no network hop on the hot
path, no dependency on Google being reachable -- and it is the documented
upgrade here. It is not the right answer FIRST for this relay: tokeninfo needs
no crypto dependency and no key-rotation handling, and both of those are
places to get verification subtly wrong. The traffic this serves is a handful
of testers behind a rate cap, so the hop costs nothing that matters. If this
relay ever serves real volume, swap :class:`GoogleTokenInfoVerifier` for a
JWKS verifier and keep everything else.

-Claude
"""

from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

# ----- CFG -------------------------------------------------------------------
TOKENINFO_URL: str = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS: frozenset[str] = frozenset(
    {"accounts.google.com", "https://accounts.google.com"}
)
VERIFY_TIMEOUT_S: float = 10.0

CLIENT_ID_ENV: str = "MDT_STEMS_RELAY_GOOGLE_CLIENT_ID"
ALLOWLIST_ENV: str = "MDT_STEMS_RELAY_ALLOWLIST"
RATE_LIMIT_ENV: str = "MDT_STEMS_RELAY_TRACKS_PER_WINDOW"
RATE_WINDOW_ENV: str = "MDT_STEMS_RELAY_WINDOW_HOURS"

DEFAULT_TRACKS_PER_WINDOW: int = 200
DEFAULT_WINDOW_HOURS: int = 24


class IdentityError(Exception):
    """The caller could not be identified. Always a 401."""


class NotAllowlisted(Exception):
    """A real Google user we did not invite. Always a 403."""


class RateCapReached(Exception):
    """A real, invited tester who has spent their share. Always a 429."""

    def __init__(self, used: int, limit: int, resets_at: datetime) -> None:
        super().__init__(
            f"rate cap reached: {used} of {limit} tracks used; "
            f"resets at {resets_at.isoformat(timespec='seconds')}"
        )
        self.used = used
        self.limit = limit
        self.resets_at = resets_at


@dataclass(frozen=True)
class Caller:
    """A verified, allowlisted tester."""

    email: str
    subject: str


# ----- gate 1: verification --------------------------------------------------
class IdentityVerifier(ABC):
    """Turns a bearer token into a verified email, or refuses.

    An ABC rather than a function so tests can inject a stand-in WITHOUT the
    production path growing a bypass branch. The relay is constructed with a
    verifier; there is no code path that runs with none.
    """

    @abstractmethod
    def verify(self, token: str) -> Caller:
        """Return the verified caller, or raise IdentityError."""


class GoogleTokenInfoVerifier(IdentityVerifier):
    """Verify against Google's tokeninfo endpoint. See the module docstring."""

    def __init__(self, client_id: str, *, http: httpx.Client | None = None) -> None:
        if not client_id.strip():
            raise ValueError(
                f"the relay needs {CLIENT_ID_ENV}; without an audience to check, "
                "any Google token from any app would be accepted"
            )
        self._client_id = client_id.strip()
        self._http = http or httpx.Client(timeout=VERIFY_TIMEOUT_S)

    def verify(self, token: str) -> Caller:
        if not token.strip():
            raise IdentityError("no identity token presented")
        try:
            response = self._http.get(TOKENINFO_URL, params={"id_token": token})
        except httpx.HTTPError as exc:
            # NOT an IdentityError: we could not CHECK the token, which is a
            # different fact from the token being bad, and conflating them
            # would tell a tester their login is broken when Google is down.
            raise RuntimeError(f"could not reach Google to verify: {exc}") from exc
        if response.status_code != 200:
            raise IdentityError("Google rejected this identity token")

        claims = response.json()
        audience = claims.get("aud")
        if audience != self._client_id:
            # The single most important check here. Without it, a token minted
            # for ANY other Google app would authenticate against this relay.
            raise IdentityError("identity token was issued for a different app")
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise IdentityError("identity token was not issued by Google")
        if str(claims.get("email_verified", "")).lower() not in {"true", "1"}:
            raise IdentityError("the Google account's email is not verified")
        try:
            expires_at = int(claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IdentityError("identity token has no usable expiry") from exc
        if expires_at <= time.time():
            raise IdentityError("identity token has expired")
        email = str(claims.get("email", "")).strip().lower()
        subject = str(claims.get("sub", "")).strip()
        if not email or not subject:
            raise IdentityError("identity token carries no email or subject")
        return Caller(email=email, subject=subject)


# ----- gate 2: the allowlist -------------------------------------------------
def load_allowlist(raw: str | None = None) -> frozenset[str]:
    """Invited testers, from a comma-separated env var, lowercased.

    An EMPTY allowlist admits nobody, and the relay refuses to start with one
    rather than defaulting to open. "No list configured" and "everybody" must
    never be the same state.
    """
    source = raw if raw is not None else os.environ.get(ALLOWLIST_ENV, "")
    emails = {
        entry.strip().lower() for entry in source.split(",") if entry.strip()
    }
    if not emails:
        raise ValueError(
            f"{ALLOWLIST_ENV} is empty. Set it to the comma-separated tester "
            "emails; an unset allowlist is refused rather than treated as "
            "'allow everyone'."
        )
    return frozenset(emails)


def assert_allowlisted(caller: Caller, allowlist: frozenset[str]) -> None:
    if caller.email not in allowlist:
        raise NotAllowlisted(
            f"{caller.email} is not on the tester allowlist for this build"
        )


# ----- gate 3: the rate cap --------------------------------------------------
@dataclass
class _Window:
    started_at: datetime
    used: int = 0


class RateCap:
    """Per-user track budget over a rolling fixed window.

    IN MEMORY, and that is a stated limitation rather than an oversight: a
    relay restart forgives everyone's usage. For a handful of testers behind
    an allowlist that is an acceptable failure mode and it keeps the first
    deployment free of a database. The moment this relay is more than that,
    swap this class for the same interface over Redis or a Modal Dict -- the
    call sites do not change.

    Counts CHARGES, taken before the work is dispatched. A separation that
    fails still cost GPU time, so refunding it would let a tester with a
    reliably-failing file spend without limit.
    """

    def __init__(
        self, limit: int | None = None, window: timedelta | None = None
    ) -> None:
        self.limit = limit if limit is not None else _env_int(
            RATE_LIMIT_ENV, DEFAULT_TRACKS_PER_WINDOW
        )
        self.window = window or timedelta(
            hours=_env_int(RATE_WINDOW_ENV, DEFAULT_WINDOW_HOURS)
        )
        if self.limit <= 0:
            raise ValueError(f"{RATE_LIMIT_ENV} must be positive, got {self.limit}")
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def _current(self, email: str, now: datetime) -> _Window:
        window = self._windows.get(email)
        if window is None or now - window.started_at >= self.window:
            window = _Window(started_at=now)
            self._windows[email] = window
        return window

    def state(self, email: str, now: datetime | None = None) -> tuple[int, datetime]:
        """(used, resets_at) without charging anything."""
        moment = now or datetime.now(UTC)
        with self._lock:
            window = self._current(email, moment)
            return window.used, window.started_at + self.window

    def charge(self, email: str, cost: int = 1, now: datetime | None = None) -> None:
        """Take ``cost`` from the budget, or raise RateCapReached."""
        moment = now or datetime.now(UTC)
        with self._lock:
            window = self._current(email, moment)
            if window.used + cost > self.limit:
                raise RateCapReached(
                    used=window.used,
                    limit=self.limit,
                    resets_at=window.started_at + self.window,
                )
            window.used += cost


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


__all__ = [
    "ALLOWLIST_ENV",
    "CLIENT_ID_ENV",
    "DEFAULT_TRACKS_PER_WINDOW",
    "DEFAULT_WINDOW_HOURS",
    "RATE_LIMIT_ENV",
    "RATE_WINDOW_ENV",
    "Caller",
    "GoogleTokenInfoVerifier",
    "IdentityError",
    "IdentityVerifier",
    "NotAllowlisted",
    "RateCap",
    "RateCapReached",
    "assert_allowlisted",
    "load_allowlist",
]
