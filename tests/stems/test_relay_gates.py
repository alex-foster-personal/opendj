"""The relay's three gates. Every one of these failing costs the maintainer money.

The relay exists because no Modal credential may ship in the .dmg (the maintainer,
Wed 19 Aug 2026). That makes the relay the single place where an untrusted
caller meets a paid GPU, and these tests are the contract that meeting is
governed by.

Single-line intent:
  - if a request with no token is served then the relay is an open GPU faucet
  - if a token minted for ANOTHER Google app authenticates then every app in
    the world with a Google login can spend the maintainer's budget
  - if an expired token authenticates then a revoked tester keeps their access
  - if a verified but uninvited Google user is served then the allowlist is
    decoration
  - if the rate cap is charged after success rather than before dispatch then
    a reliably-failing file spends without limit
  - if one tester can read another's separation then a library is disclosed
  - if the relay can be assembled with an empty allowlist then an unset
    config means 'allow everyone'
  - if a part can be downloaded before the separation succeeds then the
    client can write a bundle out of nothing

-Claude
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.stems.relay import contract as api
from apps.stems.relay.app import build_app
from apps.stems.relay.identity import (
    Caller,
    GoogleTokenInfoVerifier,
    IdentityError,
    IdentityVerifier,
    RateCap,
    RateCapReached,
    load_allowlist,
)

INVITED = "tester@example.com"
OTHER_INVITED = "second@example.com"
ALLOWLIST = frozenset({INVITED, OTHER_INVITED})
CLIENT_ID = "our-app.apps.googleusercontent.com"


class _StubVerifier(IdentityVerifier):
    """A stand-in for Google, injected as an OBJECT.

    The production gate is never disabled; it is handed a different
    implementation. That distinction is the whole reason IdentityVerifier is
    an ABC rather than an `if testing:` branch inside the real verifier.
    """

    def __init__(self, tokens: dict[str, Caller]) -> None:
        self._tokens = tokens

    def verify(self, token: str) -> Caller:
        caller = self._tokens.get(token)
        if caller is None:
            raise IdentityError("unknown token")
        return caller


def _stems_result() -> dict[str, Any]:
    return {
        "stems": {part: b"fLaC-not-real" for part in api.STEM_PARTS},
        "audio": {
            "sample_rate": 44100,
            "channels": 2,
            "frame_count": 1000,
            "bit_depth": 16,
            "codec": "flac",
        },
        "source_sha256": "0" * 64,
        "model": {"name": "htdemucs_ft", "version": "4.0.1"},
        "preset": {"tag": "t", "model": "htdemucs_ft", "overlap": 0.5,
                   "shifts": 0, "rung": 8},
    }


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = build_app(
        verifier=_StubVerifier(
            {
                "tok-invited": Caller(email=INVITED, subject="sub-1"),
                "tok-other": Caller(email=OTHER_INVITED, subject="sub-2"),
                "tok-stranger": Caller(email="nobody@example.com", subject="sub-3"),
            }
        ),
        allowlist=ALLOWLIST,
        rate_cap=RateCap(limit=3, window=timedelta(hours=1)),
        backend=lambda *_args, **_kw: _stems_result(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _start(client: TestClient, token: str, stable_id: str = "a" * 40):
    return client.post(
        api.PATH_SEPARATIONS,
        content=b"pretend-source-audio",
        headers={
            "Authorization": f"Bearer {token}",
            api.HEADER_TIER: "M",
            api.HEADER_STABLE_ID: stable_id,
        },
    )


# ----- gate 1: verification --------------------------------------------------


def test_no_token_is_refused(client: TestClient) -> None:
    """if this is served then the relay is an open GPU faucet"""
    response = client.post(api.PATH_SEPARATIONS, content=b"x")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == api.CODE_NO_TOKEN


def test_an_unverifiable_token_is_refused(client: TestClient) -> None:
    """if a token that Google does not vouch for is served then verification
    is decoration"""
    response = _start(client, "tok-forged")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == api.CODE_BAD_TOKEN


def test_a_token_for_another_app_is_refused() -> None:
    """THE audience check. Without it every Google-login app in the world can
    spend the maintainer's GPU budget with a token their own users handed them."""
    verifier = GoogleTokenInfoVerifier(
        CLIENT_ID,
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "aud": "somebody-elses-app.apps.googleusercontent.com",
                        "iss": "https://accounts.google.com",
                        "email": INVITED,
                        "email_verified": "true",
                        "sub": "sub-1",
                        "exp": str(int(time.time()) + 3600),
                    },
                )
            )
        ),
    )
    with pytest.raises(IdentityError, match="different app"):
        verifier.verify("token-for-another-app")


def test_an_expired_token_is_refused() -> None:
    """if expiry is not checked then revoking a tester never takes effect"""
    verifier = GoogleTokenInfoVerifier(
        CLIENT_ID,
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "aud": CLIENT_ID,
                        "iss": "https://accounts.google.com",
                        "email": INVITED,
                        "email_verified": "true",
                        "sub": "sub-1",
                        "exp": str(int(time.time()) - 60),
                    },
                )
            )
        ),
    )
    with pytest.raises(IdentityError, match="expired"):
        verifier.verify("stale")


def test_an_unverified_email_is_refused() -> None:
    """if email_verified is ignored then an allowlist keyed by email can be
    walked straight past with an unproven address"""
    verifier = GoogleTokenInfoVerifier(
        CLIENT_ID,
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "aud": CLIENT_ID,
                        "iss": "https://accounts.google.com",
                        "email": INVITED,
                        "email_verified": "false",
                        "sub": "sub-1",
                        "exp": str(int(time.time()) + 3600),
                    },
                )
            )
        ),
    )
    with pytest.raises(IdentityError, match="not verified"):
        verifier.verify("unverified-email")


def test_a_verifier_without_an_audience_refuses_to_exist() -> None:
    """if a blank client id were allowed then the audience check is a no-op"""
    with pytest.raises(ValueError, match="MDT_STEMS_RELAY_GOOGLE_CLIENT_ID"):
        GoogleTokenInfoVerifier("   ")


# ----- gate 2: the allowlist -------------------------------------------------


def test_a_real_google_user_we_did_not_invite_is_refused(client: TestClient) -> None:
    """if this is served then the allowlist is decoration and anyone with a
    Google account is a tester"""
    response = _start(client, "tok-stranger")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == api.CODE_NOT_ALLOWED


def test_an_empty_allowlist_is_refused_rather_than_meaning_everyone() -> None:
    """if an unset allowlist meant 'allow all' then a missing env var is a
    public GPU endpoint"""
    with pytest.raises(ValueError, match="empty"):
        load_allowlist("")
    with pytest.raises(ValueError, match="empty"):
        load_allowlist("   ,  , ")


def test_the_allowlist_is_case_insensitive() -> None:
    """if it were not then Tester@ and tester@ are two different people and
    one of them is locked out of their own invitation"""
    assert load_allowlist("Tester@Example.com") == frozenset({"tester@example.com"})


# ----- gate 3: the rate cap --------------------------------------------------


def test_the_cap_is_charged_before_dispatch_not_after_success() -> None:
    """if a failed separation were refunded then one reliably-broken file
    spends without limit, because the GPU time was still bought"""
    cap = RateCap(limit=2, window=timedelta(hours=1))
    cap.charge(INVITED)
    cap.charge(INVITED)
    with pytest.raises(RateCapReached):
        cap.charge(INVITED)
    used, _resets = cap.state(INVITED)
    assert used == 2


def test_the_cap_is_per_user(client: TestClient) -> None:
    """if it were global then one busy tester locks out every other"""
    cap = RateCap(limit=1, window=timedelta(hours=1))
    cap.charge(INVITED)
    cap.charge(OTHER_INVITED)  # must not raise
    with pytest.raises(RateCapReached):
        cap.charge(INVITED)


def test_the_window_rolls_over(client: TestClient) -> None:
    """if it never reset then a cap is a lifetime quota"""
    cap = RateCap(limit=1, window=timedelta(hours=1))
    start = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    cap.charge(INVITED, now=start)
    with pytest.raises(RateCapReached):
        cap.charge(INVITED, now=start + timedelta(minutes=59))
    cap.charge(INVITED, now=start + timedelta(hours=1, seconds=1))


def test_a_spent_cap_answers_429_naming_when_it_resets(client: TestClient) -> None:
    """if the refusal did not say when, a tester can only poll to find out"""
    for index in range(3):
        assert _start(client, "tok-invited", f"{index:040d}").status_code == 202
    response = _start(client, "tok-invited", "f" * 40)
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == api.CODE_QUOTA_SPENT
    assert detail["retry_after_s"] > 0


def test_quota_is_readable_before_spending_anything(client: TestClient) -> None:
    """the prompt states what is left rather than letting a tester find the
    limit by hitting it"""
    body = client.get(
        api.PATH_QUOTA, headers={"Authorization": "Bearer tok-invited"}
    ).json()
    assert body["email"] == INVITED
    assert body["used"] == 0
    assert body["limit"] == 3
    assert set(body["tiers_allowed"]) == {"S", "M", "L"}


# ----- ownership and readiness -----------------------------------------------


def test_one_tester_cannot_read_another_separation(client: TestClient) -> None:
    """if this leaked then a stable id and a status disclose another tester's
    library; a 404 rather than 403 because 'exists but not yours' is itself
    the disclosure"""
    created = _start(client, "tok-invited").json()
    response = client.get(
        api.PATH_SEPARATION.format(separation_id=created["separation_id"]),
        headers={"Authorization": "Bearer tok-other"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == api.CODE_UNKNOWN_SEPARATION


def test_a_separation_runs_and_yields_every_part(client: TestClient) -> None:
    """the happy path: all four parts, downloadable, once it succeeds"""
    created = _start(client, "tok-invited").json()
    state = _await_terminal(client, created["separation_id"])
    assert state["status"] == "succeeded", state["error"]
    assert set(state["parts"]) == set(api.STEM_PARTS)
    for part in api.STEM_PARTS:
        blob = client.get(
            api.PATH_SEPARATION_PART.format(
                separation_id=created["separation_id"], part=part
            ),
            headers={"Authorization": "Bearer tok-invited"},
        )
        assert blob.status_code == 200
        assert blob.content == b"fLaC-not-real"


def test_a_backend_returning_the_wrong_parts_fails_the_separation() -> None:
    """if a short part set were accepted then the client writes a bundle the
    stem reader will later reject, and the failure surfaces at play time"""
    app = build_app(
        verifier=_StubVerifier({"t": Caller(email=INVITED, subject="s")}),
        allowlist=ALLOWLIST,
        rate_cap=RateCap(limit=5, window=timedelta(hours=1)),
        backend=lambda *_a, **_k: {**_stems_result(), "stems": {"vocals": b"x"}},
    )
    with TestClient(app) as client:
        created = client.post(
            api.PATH_SEPARATIONS,
            content=b"audio",
            headers={
                "Authorization": "Bearer t",
                api.HEADER_STABLE_ID: "a" * 40,
                api.HEADER_TIER: "M",
            },
        ).json()
        state = _await_terminal(client, created["separation_id"], token="t")
        assert state["status"] == "failed"
        assert "expected" in state["error"]


def test_a_part_cannot_be_downloaded_before_success() -> None:
    """if it could then the client can build a bundle out of nothing"""
    slow = build_app(
        verifier=_StubVerifier({"t": Caller(email=INVITED, subject="s")}),
        allowlist=ALLOWLIST,
        rate_cap=RateCap(limit=5, window=timedelta(hours=1)),
        backend=_never_finishes,
    )
    with TestClient(slow) as client:
        created = client.post(
            api.PATH_SEPARATIONS,
            content=b"audio",
            headers={
                "Authorization": "Bearer t",
                api.HEADER_STABLE_ID: "a" * 40,
                api.HEADER_TIER: "M",
            },
        ).json()
        response = client.get(
            api.PATH_SEPARATION_PART.format(
                separation_id=created["separation_id"], part="vocals"
            ),
            headers={"Authorization": "Bearer t"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == api.CODE_NOT_READY


def test_an_unknown_tier_is_refused_before_any_dispatch(client: TestClient) -> None:
    """if an arbitrary tier reached the backend then a caller picks the card"""
    response = client.post(
        api.PATH_SEPARATIONS,
        content=b"audio",
        headers={
            "Authorization": "Bearer tok-invited",
            api.HEADER_TIER: "XXL",
            api.HEADER_STABLE_ID: "a" * 40,
        },
    )
    assert response.status_code == 400


def test_health_needs_no_token(client: TestClient) -> None:
    """a reachability probe that needs a tester's token cannot diagnose a
    tester who cannot sign in"""
    body = client.get(api.PATH_HEALTH).json()
    assert body["status"] == "ok"
    assert body["testers_allowlisted"] == 2


def _never_finishes(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    time.sleep(30)
    return _stems_result()


def _await_terminal(
    client: TestClient, separation_id: str, token: str = "tok-invited"
) -> dict[str, Any]:
    for _ in range(200):
        state = client.get(
            api.PATH_SEPARATION.format(separation_id=separation_id),
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        if state["status"] in {"succeeded", "failed"}:
            return state
        time.sleep(0.02)
    raise AssertionError(f"separation {separation_id} never went terminal")
