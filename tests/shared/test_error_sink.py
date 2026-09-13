"""OBS-01 Part 1: one error sink, host/sha on every record, kind=build, default off.

No network. The sink writes a local JSONL and, when telemetry is on, a Sentry
payload that tests inspect without sending. Packaged default-off is the
consent rule: fleet test builds set OPENDJ_TELEMETRY=1; outside users do not.

Regression lines:
  - if a record is missing error_id, host, or build_sha, then broken
  - if a build-failure event is not tagged kind=build, then broken
  - if a packaged (payload) build with a DSN present still defaults ON, then
    broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.telemetry import DSN_ENV, TELEMETRY_ENV, decide_telemetry
from apps.shared.telemetry.sink import (
    capture_error_event,
    post_build_failure,
    sentry_payload,
)

pytestmark = pytest.mark.requirement("OBS-01")

DSN = "https://public@o0.ingest.de.sentry.io/1"
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_error_event_carries_stable_id_host_and_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if an engine error is captured then the sink row has id, host, and sha."""
    sink = tmp_path / "opendj-error-sink.jsonl"
    monkeypatch.setenv("OPENDJ_ERROR_SINK_LOG", str(sink))
    monkeypatch.setenv("OPENDJ_HOST_LABEL", "silver")
    monkeypatch.setenv("OPENDJ_BUILD_SHA", SHA)
    monkeypatch.delenv(TELEMETRY_ENV, raising=False)
    monkeypatch.delenv(DSN_ENV, raising=False)

    event = capture_error_event(
        message="job 17 failed: stem decode",
        source_site="apps.engine_core.jobs.runner:412",
        kind="engine",
    )
    assert event.error_id.startswith("eid-")
    assert event.host == "silver"
    assert event.build_sha == SHA
    assert event.kind == "engine"

    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["error_id"] == event.error_id
    assert row["host"] == "silver"
    assert row["build_sha"] == SHA
    assert row["kind"] == "engine"
    assert event.error_id in sink.read_text(encoding="utf-8")


def test_build_failure_payload_is_tagged_kind_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a CI or headless-dmg failure is posted then the sink tags kind=build."""
    sink = tmp_path / "opendj-error-sink.jsonl"
    monkeypatch.setenv("OPENDJ_ERROR_SINK_LOG", str(sink))
    monkeypatch.setenv("OPENDJ_HOST_LABEL", "nucbox-wsl")
    monkeypatch.setenv("OPENDJ_BUILD_SHA", SHA)

    event = post_build_failure(
        message="CI failed run=123 https://github.com/example/run/123",
        source_site="build:CI",
        host="nucbox-wsl",
        build_sha=SHA,
    )
    assert event.kind == "build"
    row = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "build"
    assert row["error_id"] == event.error_id
    assert row["host"] == "nucbox-wsl"
    assert row["build_sha"] == SHA

    payload = sentry_payload(event)
    tags = payload["tags"]
    assert tags["kind"] == "build"
    assert tags["error_id"] == event.error_id
    assert tags["host"] == "nucbox-wsl"
    assert tags["build_sha"] == SHA


def test_packaged_build_defaults_off_even_with_a_dsn() -> None:
    """if the operator did not opt in then a payload build stays off.

    Fleet test builds set OPENDJ_TELEMETRY=1. Packaged default is off until
    a consent UX exists. A DSN sitting in the environment is not consent.
    """
    ship = decide_telemetry({DSN_ENV: DSN}, build_source="payload", release=SHA)
    assert ship.enabled is False
    assert ship.environment == "ship"

    fleet = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    assert fleet.enabled is True
    assert fleet.release == SHA
