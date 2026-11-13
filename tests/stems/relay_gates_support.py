"""Shared stand-ins for the relay gate tests (identity stub, backend result, client).

-Claude
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.stems.relay import contract as api
from apps.stems.relay.app import build_app
from apps.stems.relay.identity import (
    Caller,
    IdentityError,
    IdentityVerifier,
    RateCap,
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
        "preset": {"tag": "t", "model": "htdemucs_ft", "overlap": 0.5, "shifts": 0, "rung": 8},
    }


@pytest.fixture(name="client")
def _client_fixture() -> Iterator[TestClient]:
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


def _never_finishes(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    time.sleep(30)
    return _stems_result()


def _await_terminal(client: TestClient, separation_id: str, token: str = "tok-invited") -> dict[str, Any]:
    for _ in range(200):
        state = client.get(
            api.PATH_SEPARATION.format(separation_id=separation_id),
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        if state["status"] in {"succeeded", "failed"}:
            return state
        time.sleep(0.02)
    raise AssertionError(f"separation {separation_id} never went terminal")
