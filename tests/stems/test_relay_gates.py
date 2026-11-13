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
  - if a source the tier cannot encode is charged before it is refused then
    a caller pays for a separation that can never run (STEM-75)

-Claude
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.stems.relay import contract as api
from apps.stems.relay.app import build_app
from apps.stems.relay.identity import (
    Caller,
    GoogleTokenInfoVerifier,
    IdentityError,
    RateCap,
    RateCapReached,
    load_allowlist,
)
from tests.stems.relay_gates_support import (
    ALLOWLIST,
    CLIENT_ID,
    INVITED,
    OTHER_INVITED,
    _await_terminal,
    _client_fixture,  # noqa: F401  (the client fixture)
    _never_finishes,
    _start,
    _stems_result,
    _StubVerifier,
    _used,
)


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
                api.HEADER_SOURCE_NAME: "a.mp3",
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
                api.HEADER_SOURCE_NAME: "a.mp3",
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


@pytest.mark.requirement("STEM-75")
@pytest.mark.parametrize("tier", ["S", "M"])
@pytest.mark.parametrize("source_name", ["a.ogg", "a.aac", "track"])
def test_a_source_the_tier_cannot_encode_is_refused_before_the_quota_is_charged(
    client: TestClient, tier: str, source_name: str
) -> None:
    """[if] an unencodable source is charged before it is refused [then] fail, [else stop].

    If the refusal came from the backend, the caller had already paid for a
    separation that could never run (Codex P2 on #6821)."""
    response = _start(client, "tok-invited", tier=tier, source_name=source_name)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == api.CODE_BAD_REQUEST
    assert "no output-format policy" in detail["message"]
    assert _used(client) == 0


@pytest.mark.requirement("STEM-75")
@pytest.mark.parametrize(
    ("tier", "source_name"),
    [("M", "a.mp3"), ("S", "a.m4a"), ("M", "a.FLAC"), ("M", "a.wav"), ("L", "a.ogg"), ("L", "a.aac")],
)
def test_a_source_the_tier_can_encode_is_charged_and_accepted(
    client: TestClient, tier: str, source_name: str
) -> None:
    """[if] an encodable source is refused or not charged once [then] fail, [else stop].

    The control, both ways: a source with a policy, and ANY source on the
    FLAC tier, is still accepted and charged exactly once."""
    response = _start(client, "tok-invited", tier=tier, source_name=source_name)

    assert response.status_code == 202, response.text
    assert _used(client) == 1


def test_health_needs_no_token(client: TestClient) -> None:
    """a reachability probe that needs a tester's token cannot diagnose a
    tester who cannot sign in"""
    body = client.get(api.PATH_HEALTH).json()
    assert body["status"] == "ok"
    assert body["testers_allowlisted"] == 2
