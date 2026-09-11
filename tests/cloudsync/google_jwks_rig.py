"""A real key source for the Google id_token tests: RSA keys and a JWKS server.

Nothing here imitates the verifier. The keys are real 2048-bit RSA pairs, the
tokens are real RS256 JWTs signed with them, and the JWKS is served by a real
HTTP server on a free loopback port, which the hub fetches over a real
socket. The only thing standing in for Google is WHO holds the private key,
which is exactly the variable the tests need to control: a forged token is
one signed by a key the server does not publish.

The server counts its GETs, because "exactly one refetch on an unknown kid"
is a claim about requests on the wire, and a counter inside the verifier
would only report what the verifier believes it did.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

GOOGLE_TEST_CLIENT_ID: str = "opendj-test-client.apps.googleusercontent.com"
GOOGLE_TEST_ISSUER: str = "https://accounts.google.com"


@dataclass(frozen=True)
class SigningKey:
    """One RSA key pair and the ``kid`` it is published under."""

    kid: str
    private_key: rsa.RSAPrivateKey

    def public_jwk(self) -> dict[str, Any]:
        jwk: dict[str, Any] = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key())
        )
        return {**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}


def new_signing_key(kid: str | None = None) -> SigningKey:
    """A fresh RSA key. ``kid`` is unique by default so no two tests collide."""
    return SigningKey(
        kid=kid or f"kid-{secrets.token_hex(6)}",
        private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def google_claims(**overrides: Any) -> dict[str, Any]:
    """A claim set exactly as Google issues one for our client, then overrides."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": GOOGLE_TEST_ISSUER,
        "aud": GOOGLE_TEST_CLIENT_ID,
        "azp": GOOGLE_TEST_CLIENT_ID,
        "sub": "google-sub-new-user",
        "email": "new.user@example.com",
        "email_verified": True,
        "name": "New User",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def sign_id_token(key: SigningKey, claims: dict[str, Any], *, kid: str | None = None) -> str:
    """An RS256 JWT over ``claims``, signed by ``key`` and labeled ``kid``."""
    return jwt.encode(claims, key.private_key, algorithm="RS256", headers={"kid": kid or key.kid})


def unsigned_id_token(claims: dict[str, Any], *, kid: str) -> str:
    """An ``alg: none`` JWT: header and payload, empty signature. Built by hand
    so no library can quietly refuse to produce the thing under test."""

    def segment(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT', 'kid': kid})}.{segment(claims)}."


@dataclass
class JwksServer:
    """What the loopback JWKS server publishes, and what it has been asked."""

    url: str
    published: list[SigningKey] = field(default_factory=list)
    cache_control: str | None = "public, max-age=3600"
    age: str | None = None
    requests: int = 0
    #: Seconds each GET waits before answering, to hold a fetch open.
    delay_s: float = 0.0
    #: Set once a GET has arrived, before the delay, so a test can act
    #: while a fetch is provably in flight.
    fetch_started: threading.Event = field(default_factory=threading.Event)


@contextmanager
def serve_jwks(*keys: SigningKey) -> Iterator[JwksServer]:
    """Serve ``keys`` as a JWKS on a free loopback port until the block exits.

    The URL carries a random path so every server is a distinct URL, and so a
    distinct entry in the hub's process-wide JWKS cache even when the OS
    hands a later test the same port.
    """
    state = JwksServer(url="", published=list(keys))
    path = f"/{secrets.token_hex(8)}/oauth2/v3/certs"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != path:
                self.send_error(404)
                return
            state.requests += 1
            state.fetch_started.set()
            time.sleep(state.delay_s)
            body = json.dumps({"keys": [k.public_jwk() for k in state.published]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if state.cache_control is not None:
                self.send_header("Cache-Control", state.cache_control)
            if state.age is not None:
                self.send_header("Age", state.age)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)


__all__ = [
    "GOOGLE_TEST_CLIENT_ID",
    "JwksServer",
    "SigningKey",
    "google_claims",
    "new_signing_key",
    "serve_jwks",
    "sign_id_token",
    "unsigned_id_token",
]
