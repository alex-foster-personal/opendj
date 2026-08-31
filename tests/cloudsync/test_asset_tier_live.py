"""One REAL R2 round-trip against the content-addressed asset tier.

Skipped unless ``R2_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` /
``R2_SECRET_ACCESS_KEY`` are in the environment, which in practice means::

    doppler run -p music-dj-tools -c prod -- \\
        uv run pytest tests/cloudsync/test_asset_tier_live.py -m live_r2

Nothing here is mocked. The whole point of this file is that the SigV4
presigner in :mod:`apps.cloud.asset_store` is exercised against Cloudflare's
actual signature verification, which no in-memory fake can do -- a presigner
that is self-consistently wrong passes every unit test in the suite next door
and 403s in a venue.

boto3 is an opt-in extra in this repo, so the transport here is stdlib
``urllib`` over presigned PUT / ranged GET / DELETE. The object is written
under the real ``assets/<hash[:2]>/<hash>`` key for a few bytes of unique
test payload and deleted in a ``finally``; a leaked object would cost about
one ten-millionth of a cent a month, but leaving litter in the canonical
asset bucket is how a bucket stops being trustworthy.
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.cloud import asset_store

from .conftest import LIVE_R2_ENV_VARS, live_r2_config

pytestmark = [
    pytest.mark.live_r2,
    pytest.mark.requirement("CAT-04"),
]

REQUEST_TIMEOUT_SECONDS: int = 30


def _request(url: str, method: str, *, body: bytes | None = None, headers=None):
    request = urllib.request.Request(
        url, data=body, method=method, headers=headers or {}
    )
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)


def test_live_r2_round_trip_put_ranged_get_delete():
    cfg = live_r2_config()
    if cfg is None:
        pytest.skip(
            "live R2 creds absent; set "
            + ", ".join(LIVE_R2_ENV_VARS)
            + " (via `doppler run -p music-dj-tools -c prod --`) to run this."
        )

    # Unique payload so a concurrent run on another machine cannot collide,
    # and so the digest is never one already in the bucket.
    body = f"music-dj-tools asset-tier live probe {uuid.uuid4().hex}".encode()
    digest = hashlib.sha256(body).hexdigest()
    key = asset_store.asset_object_key(digest)
    assert key == f"assets/{digest[:2]}/{digest}"

    put_url = asset_store.presign_url(cfg, digest, 300, method="PUT")
    get_url = asset_store.presign_url(cfg, digest, 300, method="GET")
    delete_url = asset_store.presign_url(cfg, digest, 300, method="DELETE")

    try:
        with _request(put_url, "PUT", body=body) as response:
            assert response.status in (200, 201)

        # Ranged GET: the streaming read path depends on R2 honoring Range,
        # so assert the 206 and the exact slice, not just a 200.
        with _request(get_url, "GET", headers={"Range": "bytes=0-7"}) as response:
            assert response.status == 206
            assert response.read() == body[:8]

        with _request(get_url, "GET") as response:
            assert response.status == 200
            fetched = response.read()
        assert hashlib.sha256(fetched).hexdigest() == digest
    finally:
        try:
            with _request(delete_url, "DELETE") as response:
                assert response.status in (200, 204)
        except urllib.error.HTTPError as exc:  # pragma: no cover - cleanup
            pytest.fail(
                f"live probe object {key} could not be deleted (HTTP "
                f"{exc.code}); remove it by hand before rerunning."
            )

    # The key must be gone; a presigned GET on a deleted object is a 404.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _request(get_url, "GET")
    assert excinfo.value.code == 404


def test_live_r2_rejects_an_expired_signature():
    """A signature is time-bound, which is the reason the cache is not URL-keyed."""
    cfg = live_r2_config()
    if cfg is None:
        pytest.skip("live R2 creds absent; see the round-trip test for the how.")

    stale = datetime.now(UTC) - timedelta(seconds=1200)
    url = asset_store.presign_url(
        cfg, hashlib.sha256(b"expired probe").hexdigest(), 300, now=stale
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _request(url, "GET")
    assert excinfo.value.code == 403
