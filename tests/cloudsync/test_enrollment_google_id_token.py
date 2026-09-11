"""ADR 12 section B: the ``google_id_token`` enrollment credential, verified.

The USER path: a spoke presents a Google id_token at ``POST
/api/v1/sync/enroll`` and the hub must prove Google signed it for OUR client
before a single owner row is written. Everything here runs through the real
sync router, a real migrated sqlite hub, real RS256 signatures and a real
JWKS server on a loopback socket (:mod:`tests.cloudsync.google_jwks_rig`).
The only substitution is the JWKS URL, pointed at that server through the
same env config production reads.

Every refusal is paired with a control that makes it able to fail: the
valid-token test is the positive control for every bad-claim case (same key,
same server, one claim changed), and the forged and unsigned tests assert
that :func:`apps.webui.server.auth.decode_id_token_claims` WOULD have read
the attacker's claims, so a hub that fell back to it would have enrolled them.

[if] a token Google did not sign for our client enrolls a machine [then] fail, [else stop].
[if] a hub unable to verify answers other than 503, nothing written [then] fail, [else stop].
[if] an unknown kid on a fresh cache costs other than exactly one refetch [then] fail, [else stop].
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from apps.shared import google_oauth_client
from apps.shared.state import db as state_db
from apps.sync_hub import client, google_id_token
from apps.webui.server import auth
from tests.cloudsync.conftest import ENROLL_OWNER_EMAIL, ENROLL_OWNER_SUB, free_port
from tests.cloudsync.enrollment_helpers import http_enroll, owner_rows
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.google_jwks_rig import (
    GOOGLE_TEST_CLIENT_ID,
    JwksServer,
    SigningKey,
    google_claims,
    new_signing_key,
    serve_jwks,
    sign_id_token,
    unsigned_id_token,
)

pytestmark = pytest.mark.requirement("CAT-04")

KIND = "google_id_token"


# ----- fixtures and readers ------------------------------------------------


@pytest.fixture
def google_key() -> SigningKey:
    """The key the fake Google publishes. A fresh RSA pair per test."""
    return new_signing_key()


@pytest.fixture
def jwks(google_key: SigningKey) -> Iterator[JwksServer]:
    """A live loopback JWKS publishing ``google_key`` with max-age=3600."""
    with serve_jwks(google_key) as server:
        yield server


@pytest.fixture
def google_env(jwks: JwksServer, monkeypatch: pytest.MonkeyPatch) -> JwksServer:
    """The hub's env configured exactly as production reads it."""
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", GOOGLE_TEST_CLIENT_ID)
    monkeypatch.setenv(google_id_token.JWKS_URL_ENV_NAME, jwks.url)
    return jwks


def refused(hub: TestClientTransport, spoke_dir: Path, token: str) -> str:
    """POST ``token`` and return the refusal text; fail if it was accepted."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        http_enroll(hub, spoke_dir, name="spoke", token=token, kind=KIND)
    return str(excinfo.value)


def users_by_sub(hub_dir: Path) -> dict[str, str]:
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        return {str(s): str(e) for s, e in conn.execute("SELECT google_sub, email FROM users")}
    finally:
        conn.close()


def assert_nothing_written(hub_dir: Path) -> None:
    assert owner_rows(hub_dir) == [], "a refused enrollment wrote an owner row"
    assert users_by_sub(hub_dir) == {ENROLL_OWNER_SUB: ENROLL_OWNER_EMAIL}, (
        "a refused enrollment created or changed a users row"
    )


# ----- the valid token, which is every refusal's positive control ----------


def test_a_valid_google_id_token_enrolls_the_machine_to_its_sub(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a correctly signed token for our client does not enroll then broken"""
    token = sign_id_token(google_key, google_claims())
    body = http_enroll(enroll_hub, enroll_spoke_dir, name="spoke", token=token, kind=KIND)

    rows = owner_rows(enroll_hub_dir)
    assert len(rows) == 1
    assert rows[0]["google_sub"] == "google-sub-new-user"
    assert rows[0]["enrolled_via"] == "google_id_token"
    assert body["owner_email"] == "new.user@example.com"
    assert body["created"] is True
    assert users_by_sub(enroll_hub_dir)["google-sub-new-user"] == "new.user@example.com"
    assert google_env.requests == 1, "one enrollment on a cold cache is one JWKS fetch"


def test_a_token_for_an_existing_user_reuses_their_users_row(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a verified sub already on the hub gets a second users row then broken"""
    token = sign_id_token(google_key, google_claims(sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL))
    http_enroll(enroll_hub, enroll_spoke_dir, name="spoke", token=token, kind=KIND)

    assert owner_rows(enroll_hub_dir)[0]["google_sub"] == ENROLL_OWNER_SUB
    assert users_by_sub(enroll_hub_dir) == {ENROLL_OWNER_SUB: ENROLL_OWNER_EMAIL}


# ----- tokens Google did not sign ------------------------------------------


def test_a_token_signed_by_a_different_key_is_refused(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a token forged under Google's published kid enrolls then broken"""
    attacker = new_signing_key(kid=google_key.kid)
    claims = google_claims(sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    forged = sign_id_token(attacker, claims)

    # Control: the unverified decoder reads the forgery as the maintainer. A hub that
    # fell back to it would have enrolled this machine to him.
    assert auth.decode_id_token_claims(forged)["sub"] == ENROLL_OWNER_SUB

    message = refused(enroll_hub, enroll_spoke_dir, forged)
    assert "HTTP 401" in message and "SYNC_ENROLL_CREDENTIAL" in message
    assert "InvalidSignatureError" in message
    assert_nothing_written(enroll_hub_dir)


def test_an_unsigned_alg_none_token_is_refused(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if an alg=none token with a published kid enrolls then broken"""
    claims = google_claims(sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    unsigned = unsigned_id_token(claims, kid=google_key.kid)

    assert auth.decode_id_token_claims(unsigned)["sub"] == ENROLL_OWNER_SUB

    message = refused(enroll_hub, enroll_spoke_dir, unsigned)
    assert "HTTP 401" in message and "'none'" in message
    assert_nothing_written(enroll_hub_dir)
    assert google_env.requests == 0, "alg is refused before any key is fetched"


# ----- tokens Google signed, but not for this purpose ----------------------

_SKEW = google_id_token.CLOCK_SKEW_S

#: Case -> (claim overrides as a function of "now", expected refusal text).
#: A function, not a value, so "now" is read when the test runs rather than
#: at import: under xdist that can be minutes later, which would quietly
#: loosen the margins below.
BAD_CLAIMS: dict[str, tuple[Callable[[int], dict[str, Any]], str]] = {
    "wrong_aud": (
        lambda now: {"aud": "someone-elses-client.apps.googleusercontent.com"},
        "InvalidAudienceError",
    ),
    "multi_valued_aud": (
        lambda now: {"aud": [GOOGLE_TEST_CLIENT_ID, "attacker-client.apps.googleusercontent.com"]},
        "multi-valued aud is refused",
    ),
    # Two seconds past the allowance: pins the UPPER bound of the skew too,
    # so a leeway wider than CLOCK_SKEW_S enrolls this token and fails here.
    "expired": (
        lambda now: {"iat": now - 7200, "exp": now - _SKEW - 2},
        "ExpiredSignatureError",
    ),
    "foreign_iss": (lambda now: {"iss": "https://evil.example.com"}, "InvalidIssuerError"),
    "iat_in_future": (lambda now: {"iat": now + _SKEW + 30}, "ImmatureSignatureError"),
    "missing_exp": (lambda now: {"exp": None}, "MissingRequiredClaimError"),
    "missing_iat": (lambda now: {"iat": None}, "MissingRequiredClaimError"),
    "email_unverified": (lambda now: {"email_verified": False}, "email_verified is False"),
    "email_verified_as_string": (
        lambda now: {"email_verified": "true"},
        "email_verified is 'true'",
    ),
}


@pytest.mark.parametrize("case", list(BAD_CLAIMS), ids=list(BAD_CLAIMS))
def test_a_token_with_a_bad_claim_is_refused(
    case: str,
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a signed token with a bad aud, iss, exp, iat or email_verified enrolls then broken"""
    overrides, expected_reason = BAD_CLAIMS[case]
    token = sign_id_token(google_key, google_claims(**overrides(int(time.time()))))

    message = refused(enroll_hub, enroll_spoke_dir, token)
    assert "HTTP 401" in message
    assert expected_reason in message, message
    assert_nothing_written(enroll_hub_dir)


def test_expiry_inside_the_skew_allowance_still_enrolls(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if the named skew constant is not what exp is checked against then broken"""
    now = int(time.time())
    token = sign_id_token(google_key, google_claims(iat=now - 3600, exp=now - _SKEW // 2))
    http_enroll(enroll_hub, enroll_spoke_dir, name="spoke", token=token, kind=KIND)
    assert len(owner_rows(enroll_hub_dir)) == 1


@pytest.mark.parametrize(
    "email", [ENROLL_OWNER_EMAIL, ENROLL_OWNER_EMAIL.upper()], ids=["same_case", "upper_case"]
)
def test_a_verified_token_whose_email_belongs_to_another_sub_is_refused(
    email: str,
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a new sub can take over an existing user's email, in any letter case, then broken"""
    token = sign_id_token(google_key, google_claims(sub="another-sub", email=email))
    message = refused(enroll_hub, enroll_spoke_dir, token)
    assert "HTTP 409" in message and "SYNC_ENROLL" in message
    assert_nothing_written(enroll_hub_dir)


# ----- the JWKS cache: one refetch for an unknown kid, and no more ---------


def test_an_unknown_kid_triggers_exactly_one_refetch(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
) -> None:
    """if a kid missing from a fresh cache is not refetched exactly once then broken"""
    http_enroll(
        enroll_hub,
        enroll_spoke_dir,
        name="spoke",
        token=sign_id_token(google_key, google_claims()),
        kind=KIND,
    )
    assert google_env.requests == 1
    # Control: a known kid inside max-age is served from cache, so the
    # increments below are caused by the unknown kid, not by every request.
    http_enroll(
        enroll_hub,
        enroll_spoke_dir,
        name="spoke",
        token=sign_id_token(google_key, google_claims()),
        kind=KIND,
    )
    assert google_env.requests == 1

    never_published = new_signing_key()
    message = refused(
        enroll_hub,
        enroll_other_spoke_dir,
        sign_id_token(never_published, google_claims(sub="other-sub", email="o@example.com")),
    )
    assert "HTTP 401" in message and never_published.kid in message
    assert google_env.requests == 2, "an unknown kid costs exactly one refetch"

    rotated = new_signing_key()
    google_env.published.append(rotated)
    http_enroll(
        enroll_hub,
        enroll_other_spoke_dir,
        name="other-spoke",
        token=sign_id_token(rotated, google_claims(sub="other-sub", email="o@example.com")),
        kind=KIND,
    )
    assert google_env.requests == 3, "a rotated-in key is picked up by its one refetch"
    assert {row["google_sub"] for row in owner_rows(enroll_hub_dir)} == {
        "google-sub-new-user",
        "other-sub",
    }


def test_an_unknown_kid_on_a_cold_cache_costs_exactly_one_fetch(
    google_env: JwksServer,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if an unknown kid on a cold cache fetches the JWKS more than once then broken"""
    never_published = new_signing_key()
    message = refused(enroll_hub, enroll_spoke_dir, sign_id_token(never_published, google_claims()))
    assert "HTTP 401" in message and never_published.kid in message
    # The cold fetch IS the fresh fetch; refetching straight after it would
    # double the cost of every forged kid for no chance of a different answer.
    assert google_env.requests == 1
    assert_nothing_written(enroll_hub_dir)


# ----- verification never holds the hub's write lock -----------------------


def _write_lock_is_free(db_path: Path) -> bool:
    """True when a fresh connection can take sqlite's RESERVED (write) lock now."""
    probe = sqlite3.connect(db_path, timeout=0.2, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc), exc
        return False
    else:
        probe.execute("ROLLBACK")
        return True
    finally:
        probe.close()


def test_a_slow_jwks_fetch_does_not_hold_the_hub_write_lock(
    google_env: JwksServer,
    google_key: SigningKey,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if an enroll holds the hub's sqlite write lock across a JWKS fetch then broken"""
    db_path = client.state_db_path(enroll_hub_dir)

    # Control: the probe CAN see a held write lock, so a True below is a
    # measurement and not a probe that always says free.
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    assert _write_lock_is_free(db_path) is False
    holder.execute("ROLLBACK")
    holder.close()

    google_env.delay_s = 2.0
    token = sign_id_token(google_key, google_claims())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            http_enroll, enroll_hub, enroll_spoke_dir, name="spoke", token=token, kind=KIND
        )
        assert google_env.fetch_started.wait(10.0), "the enroll never reached the JWKS server"
        free_during_fetch = _write_lock_is_free(db_path)
        body = pending.result(timeout=30.0)

    assert free_during_fetch, "the hub held its write lock across the JWKS fetch"
    # Positive half: the slow fetch still ended in one real enrollment.
    assert google_env.requests == 1
    assert body["created"] is True
    assert len(owner_rows(enroll_hub_dir)) == 1


def test_the_jwks_cache_honors_max_age_less_age(google_key: SigningKey) -> None:
    """if a cached key set is reused past max-age minus Age, or refetched inside it, then broken"""
    now = [1000.0]
    with serve_jwks(google_key) as server:
        server.cache_control = "public, max-age=100, must-revalidate"
        server.age = "40"
        cache = google_id_token.JwksCache(server.url, clock=lambda: now[0])
        cache.signing_key(google_key.kid)
        now[0] += 59
        cache.signing_key(google_key.kid)
        assert server.requests == 1, "inside max-age minus Age is a cache hit"
        now[0] += 2
        cache.signing_key(google_key.kid)
        assert server.requests == 2, "past max-age minus Age refetches"


@pytest.mark.parametrize(
    ("cache_control", "age", "expected"),
    [
        ("public, max-age=19680, must-revalidate", None, 19680.0),
        ("max-age=100", "30", 70.0),
        ("max-age=100", "300", 0.0),
        ("no-store, max-age=100", None, 0.0),
        ("no-cache", None, 0.0),
        ("public", None, 0.0),
        (None, None, 0.0),
        ("max-age=100", "soon", 0.0),
    ],
)
def test_jwks_ttl_follows_cache_control(
    cache_control: str | None, age: str | None, expected: float
) -> None:
    """if an absent or uncacheable Cache-Control yields a positive TTL then broken"""
    assert google_id_token.jwks_ttl_s(cache_control, age) == expected


# ----- a hub that cannot verify answers 503 and accepts nothing ------------


def test_a_hub_without_a_client_id_answers_503(
    jwks: JwksServer,
    google_key: SigningKey,
    monkeypatch: pytest.MonkeyPatch,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a hub with no OAuth client id accepts or 401s a valid token then broken"""
    monkeypatch.delenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv(google_id_token.JWKS_URL_ENV_NAME, jwks.url)

    message = refused(enroll_hub, enroll_spoke_dir, sign_id_token(google_key, google_claims()))
    assert "HTTP 503" in message and "SYNC_ENROLL_KIND_UNAVAILABLE" in message
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in message
    assert jwks.requests == 0
    assert_nothing_written(enroll_hub_dir)


def test_an_unreachable_jwks_answers_503(
    google_key: SigningKey,
    monkeypatch: pytest.MonkeyPatch,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """if a hub that cannot fetch keys falls back to accepting unverified claims then broken"""
    closed_url = f"http://127.0.0.1:{free_port()}/certs"
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", GOOGLE_TEST_CLIENT_ID)
    monkeypatch.setenv(google_id_token.JWKS_URL_ENV_NAME, closed_url)

    message = refused(enroll_hub, enroll_spoke_dir, sign_id_token(google_key, google_claims()))
    assert "HTTP 503" in message and "could not fetch" in message
    assert_nothing_written(enroll_hub_dir)


@pytest.mark.parametrize(
    "url", ["http://jwks.example.com/certs", "ftp://127.0.0.1/certs", "file:///etc/certs"]
)
def test_a_jwks_url_an_attacker_could_answer_for_is_refused(url: str) -> None:
    """if signing keys may be fetched over plaintext from a remote host then broken"""
    env = {
        "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": GOOGLE_TEST_CLIENT_ID,
        google_id_token.JWKS_URL_ENV_NAME: url,
    }
    with pytest.raises(google_id_token.GoogleIdTokenVerifierUnavailable):
        google_id_token.GoogleIdTokenConfig.from_env(env)
    # Control: the production URL and a loopback URL both configure.
    ok = google_id_token.GoogleIdTokenConfig.from_env(
        {"OPENDJ_GOOGLE_OAUTH_CLIENT_ID": GOOGLE_TEST_CLIENT_ID}
    )
    assert ok.jwks_url == google_id_token.GOOGLE_JWKS_URL
    local = {**env, google_id_token.JWKS_URL_ENV_NAME: "http://127.0.0.1:9/certs"}
    assert google_id_token.GoogleIdTokenConfig.from_env(local).jwks_url.startswith(
        "http://127.0.0.1"
    )


def test_the_hub_and_sign_in_read_the_same_client_id_names() -> None:
    """if the hub pins aud to a different env var than sign-in reads then broken"""
    assert auth.CLIENT_ID_ENV_NAMES is google_oauth_client.CLIENT_ID_ENV_NAMES
    config = google_id_token.GoogleIdTokenConfig.from_env(
        {auth.CLIENT_ID_ENV_NAMES[0]: GOOGLE_TEST_CLIENT_ID}
    )
    assert config.client_id == GOOGLE_TEST_CLIENT_ID
