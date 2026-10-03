"""The frontend's stem-probe fixture is what the real stems route answers (STEM-37).

``apps/webui/frontend/tests/unit/stem-hydrating-probe.test.mjs`` feeds
``probeStemArtifact`` and ``awaitStemArtifact`` from
``apps/webui/frontend/tests/fixtures/stem-route-answers.json``. This module
re-captures every entry from the live FastAPI route, through the real hydration
worker against the in-memory asset store the other hydration tests use, so a
change to the wire contract fails here instead of leaving the frontend tests
green against a payload the server no longer sends.

* [if] a bundle is indexed but not local [then] the route answers the fixture's
  ``hydrating`` entry, and once hydrated its ``ready`` manifest
* [if] no bundle exists and no transport is configured [then] it answers
  ``not_found`` (message compared up to the machine-specific roots)
* [if] the indexed bundle cannot be fetched [then] it answers
  ``hydration_failed`` with HTTP 502

[if] the route's stem answers drift from the frontend fixture [then] this fails, [else stop].
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

import apps.webui.server.routes.stems as stems_module
from apps.cloud.stem_index import save_cached_index
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.webui.test_stems_hydration import _cfg, _client, _seed_bundle

pytestmark = pytest.mark.requirement("STEM-37")

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "apps/webui/frontend/tests/fixtures/stem-route-answers.json"
)
SID = "sid-1"


@pytest.fixture(autouse=True)
def _reset_hydration_module_state():
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()
    yield
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _settle(client) -> tuple[int, dict[str, Any]]:
    deadline = time.time() + 5
    while True:
        resp = client.get(f"/api/v1/tracks/{SID}/stems")
        body = resp.json()
        if resp.status_code != 200 or not body.get("hydrating") or time.time() > deadline:
            return resp.status_code, body
        time.sleep(0.02)


def test_hydrating_then_ready_match_the_live_route(tmp_path: Path) -> None:
    fixture = _fixture()
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(tmp_path / "data", {SID: _seed_bundle(s3, cfg, SID)})
    with _client(
        tmp_path / "stems", data_dir=tmp_path / "data", hydration_cfg=cfg, hydration_s3=s3
    ) as client:
        first = client.get(f"/api/v1/tracks/{SID}/stems")
        first_body = first.json()
        # The fetch races this read, so the two live counters are whatever the
        # worker has written so far. Their presence and type are the contract;
        # files_total is fixed by the index and is compared exactly.
        live = first_body["progress"]
        assert isinstance(live["files_done"], int) and live["files_done"] >= 0
        assert isinstance(live["bytes_done"], int) and live["bytes_done"] >= 0
        first_body["progress"] = {**live, "files_done": 0, "bytes_done": 0}
        assert (first.status_code, first_body) == (
            fixture["hydrating"]["status"],
            fixture["hydrating"]["body"],
        )
        assert _settle(client) == (fixture["ready"]["status"], fixture["ready"]["body"])


def test_not_found_matches_the_live_route(tmp_path: Path) -> None:
    expected = _fixture()["not_found"]
    with _client(tmp_path / "stems", data_dir=tmp_path / "data") as client:
        resp = client.get(f"/api/v1/tracks/{SID}/stems")
    body = resp.json()
    assert resp.status_code == expected["status"]
    assert body["message"].startswith(expected["body"]["message"])
    assert {k: v for k, v in body.items() if k != "message"} == {
        k: v for k, v in expected["body"].items() if k != "message"
    }


def test_hydration_failed_matches_the_live_route(tmp_path: Path) -> None:
    expected = _fixture()["hydration_failed"]
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, SID)
    entry["vocals.wav"] = "f" * 64  # indexed, never pushed
    save_cached_index(tmp_path / "data", {SID: entry})
    with _client(
        tmp_path / "stems", data_dir=tmp_path / "data", hydration_cfg=cfg, hydration_s3=s3
    ) as client:
        assert _settle(client) == (expected["status"], expected["body"])
