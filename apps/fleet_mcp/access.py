"""Cloudflare Access verification for the remote ``dispatch`` endpoint.

The remote endpoint can open ``queue:ready`` issues, which the nucbox
dispatcher claims and BUILDS without a human in the loop. That makes it a
write surface with autonomous consequences, so it verifies the Access JWT
itself rather than trusting that ``cloudflared`` was configured to.

Why not presence-only, as :mod:`apps.webui.server.share_gate` does: that
module's own docstring calls itself "the app-level belt" behind Cloudflare
Access as "the outer door", and for a read-mostly library share that is a
fair trade. Here the question ".claude/rules/verification.md" makes us ask --
what could satisfy this check without satisfying its intent? -- has an easy
answer: any process that can reach the loopback port can SET
``cf-access-authenticated-user-email`` to anything. A tunnel config missing
``originRequest.access.required`` would then serve this endpoint naked and
nothing would notice. Verifying the signature, the audience and the issuer
closes that, and costs one cached JWKS fetch.

Fail-closed everywhere: unset configuration refuses to start the server, and
an unverifiable request is refused rather than downgraded.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

TEAM_ENV = "DISPATCH_MCP_ACCESS_TEAM"
AUD_ENV = "DISPATCH_MCP_ACCESS_AUD"
EMAILS_ENV = "DISPATCH_MCP_ACCESS_EMAILS"

# Cloudflare sets both; the JWT is the one that carries proof.
ACCESS_JWT_HEADER = "cf-access-jwt-assertion"
ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"

_ALGORITHMS = ("RS256",)
_JWKS_CACHE_LIFESPAN_S = 300


class AccessRefusal(Exception):
    """A request did not prove an allowed identity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AccessNotConfigured(RuntimeError):
    """The server was asked to serve remotely without an Access configuration."""


@dataclass(frozen=True)
class AccessConfig:
    """Team, audience and allowlist. Every field is required."""

    team: str
    aud: str
    emails: frozenset[str]

    @property
    def issuer(self) -> str:
        return f"https://{self.team}.cloudflareaccess.com"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> AccessConfig:
        """Build from the service environment file, or refuse to start.

        An empty allowlist is a configuration error, not "allow everyone":
        `CLOUDFLARE_ACCESS.md` says never use an Everyone policy, and a
        default-open remote write surface is the outcome this whole module
        exists to make impossible.
        """
        env = os.environ if environ is None else environ
        team = env.get(TEAM_ENV, "").strip()
        aud = env.get(AUD_ENV, "").strip()
        raw_emails = env.get(EMAILS_ENV, "")
        emails = frozenset(
            part.strip().lower() for part in raw_emails.split(",") if part.strip()
        )
        missing = [
            name
            for name, value in ((TEAM_ENV, team), (AUD_ENV, aud), (EMAILS_ENV, raw_emails.strip()))
            if not value
        ]
        if missing:
            raise AccessNotConfigured(
                "remote serving needs "
                + ", ".join(missing)
                + "; put them in the root-owned service environment file, never in a worktree .env"
            )
        if not emails:
            raise AccessNotConfigured(f"{EMAILS_ENV} parsed to no addresses: {raw_emails!r}")
        return cls(team=team, aud=aud, emails=emails)


class AccessVerifier:
    """Verifies Access JWTs against the team's rotating public keys."""

    def __init__(self, config: AccessConfig, *, jwk_client: Any | None = None) -> None:
        self.config = config
        self._jwks = jwk_client or PyJWKClient(
            config.jwks_url, cache_keys=True, lifespan=_JWKS_CACHE_LIFESPAN_S
        )

    def verify(self, headers: Mapping[str, str]) -> str:
        """Return the authenticated email, or raise :class:`AccessRefusal`.

        The email is taken from the SIGNED token, never from the
        ``cf-access-authenticated-user-email`` header, which is unsigned and
        therefore only a hint.
        """
        token = _header(headers, ACCESS_JWT_HEADER)
        if not token:
            raise AccessRefusal(
                "access_jwt_missing",
                f"no {ACCESS_JWT_HEADER}; this endpoint is reachable only through "
                "Cloudflare Access",
            )
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(_ALGORITHMS),
                audience=self.config.aud,
                issuer=self.config.issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError as error:
            raise AccessRefusal("access_jwt_invalid", f"Access token rejected: {error}") from error
        except Exception as error:  # a JWKS fetch failure must not read as allowed
            raise AccessRefusal(
                "access_keys_unavailable",
                f"could not reach {self.config.jwks_url} to verify the token: {error}",
            ) from error

        email = str(claims.get("email", "")).strip().lower()
        if not email:
            raise AccessRefusal("access_email_missing", "the Access token carries no email claim")
        if email not in self.config.emails:
            raise AccessRefusal(
                "access_email_refused", f"{email} is not on this endpoint's allowlist"
            )
        return email


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value.strip()
    return ""
