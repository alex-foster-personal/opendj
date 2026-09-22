"""Cloudflare Access verification for the remote endpoint (AGENT-17).

Every test signs a REAL token with a real key and hands it to the real
verifier. The forged-header case is the one that matters: it is what an
attacker on the box, or a tunnel missing `originRequest.access.required`,
actually sends.
"""

from __future__ import annotations

import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from apps.fleet_mcp.access import (
    ACCESS_EMAIL_HEADER,
    ACCESS_JWT_HEADER,
    AccessConfig,
    AccessNotConfigured,
    AccessRefusal,
    AccessVerifier,
)

TEAM = "example-team"
AUD = "a" * 64
ALLOWED = "dev@example.com"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture
def config():
    return AccessConfig(team=TEAM, aud=AUD, emails=frozenset({ALLOWED}))


class _StaticJWKClient:
    """Stands in for the team's JWKS endpoint with one known key."""

    def __init__(self, public_key):
        self._key = public_key

    def get_signing_key_from_jwt(self, token):  # one key in this suite
        return type("Key", (), {"key": self._key})()


def _token(key, *, aud=AUD, iss=f"https://{TEAM}.cloudflareaccess.com", email=ALLOWED, age_s=0):
    now = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=age_s)
    return jwt.encode(
        {
            "aud": aud,
            "iss": iss,
            "email": email,
            "iat": now,
            "exp": now + dt.timedelta(minutes=30),
        },
        key,
        algorithm="RS256",
    )


def _verifier(config, public_key):
    return AccessVerifier(config, jwk_client=_StaticJWKClient(public_key))


@pytest.mark.requirement("AGENT-17")
def test_a_properly_signed_token_is_accepted(config, keypair):
    """[if] Access signs a token for an allowed email [then] the request passes, [else stop]

    The positive control: without it, every refusal below could be a verifier
    that simply rejects everything.
    """
    private, public = keypair
    email = _verifier(config, public).verify({ACCESS_JWT_HEADER: _token(private)})
    assert email == ALLOWED


@pytest.mark.requirement("AGENT-17")
def test_a_forged_identity_header_alone_proves_nothing(config, keypair):
    """[if] only the unsigned email header is set [then] the request is refused, [else stop]

    This is the whole reason the module verifies rather than checks presence:
    any local process can set this header, and a tunnel missing
    `originRequest.access.required` forwards whatever the client sent.
    """
    _, public = keypair
    with pytest.raises(AccessRefusal) as caught:
        _verifier(config, public).verify({ACCESS_EMAIL_HEADER: ALLOWED})
    assert caught.value.code == "access_jwt_missing"


@pytest.mark.requirement("AGENT-17")
def test_the_signed_email_wins_over_the_unsigned_header(config, keypair):
    """[if] the headers disagree [then] the signed claim decides, [else stop]"""
    private, public = keypair
    headers = {
        ACCESS_JWT_HEADER: _token(private, email=ALLOWED),
        ACCESS_EMAIL_HEADER: "attacker@example.net",
    }
    assert _verifier(config, public).verify(headers) == ALLOWED


@pytest.mark.requirement("AGENT-17")
def test_a_token_for_another_application_is_refused(config, keypair):
    """[if] the audience is another Access app [then] the request is refused, [else stop]

    Without the aud check, any token from the same team -- including one for a
    read-only share app -- would open this write endpoint.
    """
    private, public = keypair
    with pytest.raises(AccessRefusal) as caught:
        _verifier(config, public).verify({ACCESS_JWT_HEADER: _token(private, aud="b" * 64)})
    assert caught.value.code == "access_jwt_invalid"


@pytest.mark.requirement("AGENT-17")
def test_a_token_from_another_team_is_refused(config, keypair):
    """[if] the issuer is another Cloudflare team [then] the request is refused, [else stop]"""
    private, public = keypair
    forged = _token(private, iss="https://someone-else.cloudflareaccess.com")
    with pytest.raises(AccessRefusal) as caught:
        _verifier(config, public).verify({ACCESS_JWT_HEADER: forged})
    assert caught.value.code == "access_jwt_invalid"


@pytest.mark.requirement("AGENT-17")
def test_a_token_signed_by_the_wrong_key_is_refused(config, keypair):
    """[if] the signature does not match the team's key [then] refuse, [else stop]"""
    _, public = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AccessRefusal) as caught:
        _verifier(config, public).verify({ACCESS_JWT_HEADER: _token(other)})
    assert caught.value.code == "access_jwt_invalid"


@pytest.mark.requirement("AGENT-17")
def test_an_expired_token_is_refused(config, keypair):
    """[if] the token has expired [then] refuse, [else stop]"""
    private, public = keypair
    with pytest.raises(AccessRefusal) as caught:
        _verifier(config, public).verify({ACCESS_JWT_HEADER: _token(private, age_s=7200)})
    assert caught.value.code == "access_jwt_invalid"


@pytest.mark.requirement("AGENT-17")
def test_a_valid_token_for_an_unlisted_email_is_refused(config, keypair):
    """[if] the signature is good but the email is not allowlisted [then] refuse, [else stop]

    A correct Access session for a DIFFERENT person is the case the allowlist
    exists for; the signature being valid is not the question.
    """
    private, public = keypair
    with pytest.raises(AccessRefusal) as caught:
        _verifier(config, public).verify(
            {ACCESS_JWT_HEADER: _token(private, email="someone@example.net")}
        )
    assert caught.value.code == "access_email_refused"


@pytest.mark.requirement("AGENT-17")
def test_an_unreachable_jwks_endpoint_is_refused_not_allowed(config):
    """[if] the team's keys cannot be fetched [then] refuse, [else stop]

    An unmeasured signature is not a valid one. This is the fail-closed half
    of the verification rule: a probe that cannot measure never returns a pass.
    """

    class _Unreachable:
        def get_signing_key_from_jwt(self, token):
            raise OSError("no route to host")

    verifier = AccessVerifier(config, jwk_client=_Unreachable())
    with pytest.raises(AccessRefusal) as caught:
        verifier.verify({ACCESS_JWT_HEADER: "whatever"})
    assert caught.value.code == "access_keys_unavailable"


@pytest.mark.requirement("AGENT-17")
@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"DISPATCH_MCP_ACCESS_TEAM": TEAM},
        {"DISPATCH_MCP_ACCESS_TEAM": TEAM, "DISPATCH_MCP_ACCESS_AUD": AUD},
        {
            "DISPATCH_MCP_ACCESS_TEAM": TEAM,
            "DISPATCH_MCP_ACCESS_AUD": AUD,
            "DISPATCH_MCP_ACCESS_EMAILS": "   ",
        },
    ],
)
def test_incomplete_configuration_refuses_to_start(environ):
    """[if] any Access setting is unset [then] the server refuses to start, [else stop]

    Never "start with no allowlist and let everyone in", which is what a
    default-open remote write surface would be.
    """
    with pytest.raises(AccessNotConfigured):
        AccessConfig.from_environ(environ)


@pytest.mark.requirement("AGENT-17")
def test_a_complete_configuration_is_accepted_and_normalized():
    """[if] all three settings are present [then] the config builds, [else stop]"""
    config = AccessConfig.from_environ(
        {
            "DISPATCH_MCP_ACCESS_TEAM": TEAM,
            "DISPATCH_MCP_ACCESS_AUD": AUD,
            "DISPATCH_MCP_ACCESS_EMAILS": f" {ALLOWED.upper()} , second@example.com ",
        }
    )
    assert config.emails == frozenset({ALLOWED, "second@example.com"})
    assert config.jwks_url == f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/certs"
