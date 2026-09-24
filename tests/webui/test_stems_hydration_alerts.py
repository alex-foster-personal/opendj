"""A stem bundle the index promises but hydration cannot fetch reaches the error sink (STEM-38).

The manifest and part routes already answer 502 for this (STEM-16), but an
HTTP error never becomes a Sentry event (the SDK is configured with
``failed_request_status_codes=set()``), so a spoke could fail every bundle
without anyone hearing about it. An ERROR log record is what the engine
warning log forwards to the error sink and Sentry.

* [if] hydration of an indexed bundle fails [then] one ERROR record names
  the code and the stable_id
* [if] hydration succeeds [then] no ERROR record is written (control)
* [if] the hub is merely unreachable [then] the record is WARNING, not ERROR
  (control: a laptop offline at a venue is expected and must not page)

[if] an indexed bundle cannot be hydrated [then] an ERROR record reaches the sink, [else stop].
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

import apps.webui.server.routes.stems as stems_module
from apps.cloud.stem_index import save_cached_index
from apps.cloud.stem_source import STEM_HUB_INDEX_FAILED, STEM_HUB_UNREACHABLE
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.webui.test_stems_hydration import _cfg, _client, _seed_bundle

pytestmark = pytest.mark.requirement("STEM-38")

LOGGER = "apps.webui.server.routes.stems"


@pytest.fixture(autouse=True)
def _reset_hydration_module_state():
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()
    yield
    stems_module._INFLIGHT.clear()
    stems_module._LAST_HYDRATE_ERROR.clear()


def _poll_manifest(client, stable_id: str):
    deadline = time.time() + 5
    resp = None
    while time.time() < deadline:
        resp = client.get(f"/api/v1/tracks/{stable_id}/stems")
        if resp.status_code != 200 or not resp.json().get("hydrating"):
            return resp
        time.sleep(0.02)
    return resp


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.ERROR]


def test_failed_hydration_logs_one_error_naming_code_and_track(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "broken-track")
    entry["vocals.wav"] = "f" * 64  # never actually pushed
    data_dir = tmp_path / "data"
    save_cached_index(data_dir, {"broken-track": entry})

    caplog.set_level(logging.WARNING, logger=LOGGER)
    with _client(
        tmp_path / "stems", data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3
    ) as client:
        resp = _poll_manifest(client, "broken-track")
    assert resp is not None and resp.status_code == 502
    errors = _errors(caplog)
    assert len(errors) == 1, [r.getMessage() for r in caplog.records]
    message = errors[0].getMessage()
    assert "STEM_BUNDLE_HYDRATION_FAILED" in message
    assert "broken-track" in message


def test_successful_hydration_logs_no_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, "good-track")
    data_dir = tmp_path / "data"
    save_cached_index(data_dir, {"good-track": entry})

    caplog.set_level(logging.WARNING, logger=LOGGER)
    with _client(
        tmp_path / "stems", data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3
    ) as client:
        resp = _poll_manifest(client, "good-track")
    assert resp is not None and resp.status_code == 200
    assert resp.json().get("stable_id") == "good-track"
    assert "hydrating" not in resp.json() or resp.json()["hydrating"] is False
    assert _errors(caplog) == []


def test_unreachable_hub_is_a_warning_and_a_hub_5xx_is_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    stems_module._log_hydration_failure("sid-a", STEM_HUB_UNREACHABLE, "connection refused")
    stems_module._log_hydration_failure(
        "sid-b", STEM_HUB_INDEX_FAILED, "bundle presign: hub answered HTTP 503"
    )
    levels = {
        r.getMessage().split("stable_id=")[1].split(":")[0]: r.levelno for r in caplog.records
    }
    assert levels == {"sid-a": logging.WARNING, "sid-b": logging.ERROR}
