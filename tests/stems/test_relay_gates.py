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

import copy
import functools
import io
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, create_model

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


CURRENT_CLIENT_CODECS = ",".join(sorted(api.STEM_CODEC_FILES))
"""What ``apps.stems.relay.client.separate_one`` sends; None is a client from before STEM-76."""


def _start(
    client: TestClient,
    token: str,
    stable_id: str = "a" * 40,
    *,
    tier: str = "M",
    source_name: str = "a.mp3",
    codecs: str | None = CURRENT_CLIENT_CODECS,
):
    headers = {
        "Authorization": f"Bearer {token}",
        api.HEADER_TIER: tier,
        api.HEADER_STABLE_ID: stable_id,
        api.HEADER_SOURCE_NAME: source_name,
    }
    if codecs is not None:
        headers[api.HEADER_STEM_CODECS] = codecs
    return client.post(api.PATH_SEPARATIONS, content=b"pretend-source-audio", headers=headers)


def _used(client: TestClient, token: str = "tok-invited") -> int:
    body = client.get(api.PATH_QUOTA, headers={"Authorization": f"Bearer {token}"}).json()
    return int(body["used"])


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


# ----- round 12: the Modal backend is supplied by the entrypoint, never defaulted -----


def test_the_relay_refuses_to_assemble_without_a_backend() -> None:
    with pytest.raises(RuntimeError, match=r"scripts\.stems_relay_app"):
        build_app(allowlist=ALLOWLIST)


def test_the_entrypoint_imports_without_modal_and_names_the_backend() -> None:
    import importlib

    entrypoint = importlib.import_module("scripts.stems_relay_app")
    assert callable(entrypoint.build_relay_app)
    assert callable(entrypoint.modal_backend)


# ----- STEM-76: the relay carries the codec the farm actually encoded -----


@functools.cache
def _farm_encoded(codec: str) -> dict[str, Any]:
    """A separation result whose parts and audio block come from the farm's
    own encoder (``scripts.farm_stem_encode.encode_stems`` and
    ``audio_block``, the calls ``separate_track`` makes after demucs) run on
    real audio, not invented bytes (Codex P1 on #6821). Only the GPU half,
    demucs on Modal, is not run here: CI has no Modal credential, so that
    half stays open on STEM-75/76 until the redeployed relay serves a real
    MP3 separation."""
    # Plain imports: every caller is marked ``requires_audio_stack``, so a
    # host without numpy or soundfile reports UNAVAILABLE from conftest
    # before this runs, never a quiet skip (Codex P1 on #6821).
    import numpy as np

    from scripts import farm_stem_encode as fse

    rate, frames = 44100, 44100 * 2
    rng = np.random.default_rng(76)
    arrays = {part: (rng.standard_normal((frames, 2)) * 0.1).astype(np.float32) for part in api.STEM_PARTS}
    mix = sum(arrays.values()) / len(arrays)
    with tempfile.TemporaryDirectory() as tmp:
        source_bytes = len(fse.encode_mp3(mix, rate, Path(tmp) / "source.mp3", cbr_kbps=192))
    stems, size_policy = fse.encode_stems(
        arrays,
        rate,
        codec,
        source_bytes=source_bytes,
        source_kbps=fse.effective_bitrate_kbps(source_bytes, frames / rate),
    )
    result = _stems_result()
    result["stems"] = stems
    result["size_policy"] = size_policy
    result["audio"] = fse.audio_block(rate, 2, frames, codec)
    return result


def _relay_with(backend: Any) -> TestClient:
    return TestClient(
        build_app(
            verifier=_StubVerifier({"t": Caller(email=INVITED, subject="s")}),
            allowlist=ALLOWLIST,
            rate_cap=RateCap(limit=5, window=timedelta(hours=1)),
            backend=backend,
        )
    )


def _start_as_t(client: TestClient) -> str:
    created = client.post(
        api.PATH_SEPARATIONS,
        content=b"audio",
        headers={
            "Authorization": "Bearer t",
            api.HEADER_STABLE_ID: "a" * 40,
            api.HEADER_TIER: "M",
            api.HEADER_SOURCE_NAME: "a.mp3",
            api.HEADER_STEM_CODECS: CURRENT_CLIENT_CODECS,
        },
    ).json()
    return str(created["separation_id"])


@pytest.mark.requirement("STEM-76")
@pytest.mark.requires_audio_stack
@pytest.mark.parametrize(
    ("codec", "media_type", "ext", "sf_format"),
    [("mp3", "audio/mpeg", "mp3", "MP3"), ("flac", "audio/flac", "flac", "FLAC")],
)
def test_parts_are_served_and_saved_under_the_codec_the_farm_encoded(
    codec: str, media_type: str, ext: str, sf_format: str
) -> None:
    """The bundle must not name its bytes wrong.

    [if] MP3 parts are served or saved as FLAC [then] fail, [else stop].
    """
    import soundfile as sf

    from apps.stems.bundle_publish import stem_manifest
    from apps.stems.relay.client import separate_one

    result = _farm_encoded(codec)
    with _relay_with(lambda *_a, **_k: copy.deepcopy(result)) as client:
        out = separate_one(
            client,
            "",
            "t",
            audio_bytes=b"audio",
            stable_id="a" * 40,
            source_name="a.mp3",
            tier_key="M",
            deadline_s=30.0,
        )
        separation_id = _start_as_t(client)
        _await_terminal(client, separation_id, token="t")
        blob = client.get(
            api.PATH_SEPARATION_PART.format(separation_id=separation_id, part="vocals"),
            headers={"Authorization": "Bearer t"},
        )
    assert out["stem_ext"] == ext
    assert out["stem_codec"] == ext
    assert out["stems"]["vocals"] == result["stems"]["vocals"]
    # The farm's ladder audit survives the relay into the manifest (Codex on #6821).
    assert result["size_policy"], "the farm encoder produced no size policy to carry"
    assert out["size_policy"] == result["size_policy"]
    assert stem_manifest("a" * 40, "/music/a.mp3", out, {"model": "htdemucs_ft"})["size_policy"] == result["size_policy"]
    assert ("bit_depth" in out["audio"]) == (ext == "flac")
    assert blob.status_code == 200
    assert blob.headers["content-type"] == media_type
    # The bytes the relay served are what the media type claims: they decode.
    info = sf.info(io.BytesIO(blob.content))
    assert info.format == sf_format
    assert (info.samplerate, info.channels) == (44100, 2)


@pytest.mark.requirement("STEM-76")
@pytest.mark.requires_audio_stack
def test_a_codec_the_client_cannot_write_fails_the_separation() -> None:
    """[if] a codec the client cannot write succeeds [then] fail, [else stop].

    Real farm MP3 parts, labeled with a codec outside STEM_CODEC_FILES."""
    opus = copy.deepcopy(_farm_encoded("mp3"))
    opus["audio"]["codec"] = "opus"
    with _relay_with(lambda *_a, **_k: opus) as client:
        state = _await_terminal(client, _start_as_t(client), token="t")
    assert state["status"] == "failed"
    assert "opus" in state["error"]


# ----- STEM-76: a client from before STEM-76 still gets FLAC -----


def _relay_recording(asked: list[str], result: dict[str, Any] | None = None) -> TestClient:
    def backend(_audio: bytes, _sid: str, _name: str, _tier: str, stem_codec: str) -> dict[str, Any]:
        asked.append(stem_codec)
        return copy.deepcopy(result) if result is not None else _stems_result()

    return _relay_with(backend)


@pytest.mark.requirement("STEM-76")
@pytest.mark.parametrize(
    ("codecs", "source_name", "expected"),
    [
        (None, "a.mp3", "flac"),
        ("flac", "a.m4a", "flac"),
        (None, "a.ogg", "flac"),
        (CURRENT_CLIENT_CODECS, "a.mp3", "by-source"),
        ("mp3 , flac", "a.wav", "by-source"),
    ],
    ids=["legacy-mp3", "flac-only-m4a", "legacy-ogg", "current-mp3", "current-wav-spaced"],
)
def test_the_farm_is_asked_for_flac_unless_the_client_writes_every_codec(
    codecs: str | None, source_name: str, expected: str
) -> None:
    """[if] a client that names every part .flac is sent MP3 parts [then] fail, [else stop].

    Control: a current client still gets the tier's policy.

    An installed client from before STEM-76 sends no codecs header and saves
    whatever the relay returns as ``.flac``, so an MP3 answer was spent quota
    and a bundle refused at play time (Codex P1 on #6821). MUTATION TARGETS:
    route every client by the tier's policy (the legacy rows get
    ``by-source``), or send every client FLAC (the current rows get
    ``flac``).
    """
    asked: list[str] = []
    with _relay_recording(asked) as client:
        response = _start(client, "t", source_name=source_name, codecs=codecs)
        assert response.status_code == 202, response.text
        assert _await_terminal(client, response.json()["separation_id"], token="t")["status"] == "succeeded"
    assert asked == [expected]


@pytest.mark.requirement("STEM-76")
@pytest.mark.requires_audio_stack
def test_mp3_parts_for_a_flac_only_client_fail_the_separation() -> None:
    """[if] a backend answers a FLAC-only client with MP3 parts [then] the separation fails, [else stop].

    MUTATION TARGET: drop the relay's check of the returned codec against
    the one it asked for, and the legacy client is told it succeeded.
    """
    asked: list[str] = []
    with _relay_recording(asked, _farm_encoded("mp3")) as client:
        response = _start(client, "t", codecs=None)
        state = _await_terminal(client, response.json()["separation_id"], token="t")
    assert asked == ["flac"]
    assert state["status"] == "failed"
    assert "asked the backend for flac" in state["error"]


def test_the_shipped_client_lists_every_codec_it_writes() -> None:
    """[if] the client stops advertising MP3 [then] the relay would hand it FLAC forever, [else stop]."""
    import inspect

    from apps.stems.relay import client as relay_client

    assert "api.HEADER_STEM_CODECS: \",\".join(sorted(api.STEM_CODEC_FILES))" in inspect.getsource(relay_client.separate_one)


def _legacy_state_model() -> type[BaseModel]:
    """``SeparationState`` as a client from before STEM-76 has it: no ``size_policy``, extras forbidden."""
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in api.SeparationState.model_fields.items()
        if name != "size_policy"
    }
    return create_model("LegacySeparationState", __config__=ConfigDict(extra="forbid"), **fields)


@pytest.mark.requirement("STEM-76")
@pytest.mark.parametrize("codecs", [None, CURRENT_CLIENT_CODECS], ids=["legacy", "current"])
def test_a_poll_carries_size_policy_only_for_a_client_that_knows_it(
    client: TestClient, codecs: str | None
) -> None:
    """[if] a client from before STEM-76 polls [then] its answer carries no ``size_policy`` key, [else stop].

    Control: a current client gets it.

    The old client parses every poll with ``extra="forbid"``, so even a null
    ``size_policy`` failed its poll after the quota was spent (Codex P1 on
    #6821). MUTATION TARGETS: always send the key (the legacy rows fail to
    parse), or never send it (the current rows lose the farm's audit).
    """
    response = _start(client, "tok-invited", codecs=codecs)
    assert response.status_code == 202, response.text
    separation_id = response.json()["separation_id"]
    queued = client.get(
        api.PATH_SEPARATION.format(separation_id=separation_id),
        headers={"Authorization": "Bearer tok-invited"},
    ).json()
    terminal = _await_terminal(client, separation_id)
    assert terminal["status"] == "succeeded"
    for body in (queued, terminal):
        if codecs is None:
            assert "size_policy" not in body
            _legacy_state_model().model_validate(body)
        else:
            assert "size_policy" in body
            api.SeparationState.model_validate(body)
