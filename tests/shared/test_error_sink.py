"""OBS-01 Part 1: one error sink, host/sha on every record, kind=build, default off.

No network. The sink writes a local JSONL and, when telemetry is on, a Sentry
payload that tests inspect without sending. Packaged default-off is the
consent rule: fleet test builds set OPENDJ_TELEMETRY=1; outside users do not.

Regression lines:
  - if a record is missing error_id, host, or build_sha, then broken
  - if a build-failure event is not tagged kind=build, then broken
  - if a packaged (payload) build with a DSN present still defaults ON, then
    broken

[if] the sink captures an event, or a build has a DSN [then] ids carry, telemetry off, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.telemetry import DSN_ENV, TELEMETRY_ENV, decide_telemetry
from apps.shared.telemetry import sink as sink_module
from apps.shared.telemetry.sink import (
    capture_error_event,
    make_event,
    post_build_failure,
    reset_rate_state_for_tests,
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


@pytest.fixture(autouse=True)
def _reset_sink_rate_state() -> None:
    reset_rate_state_for_tests()


def test_sink_rotates_when_next_record_exceeds_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "error-sink.jsonl"
    sink.write_text("x" * 8, encoding="utf-8")
    monkeypatch.setattr(sink_module, "SINK_MAX_BYTES", 10)
    event = make_event(
        message="rotation probe",
        source_site="tests:sink:rotation",
        kind="engine",
    )
    sink_module.append_sink(event, sink)
    archives = list(tmp_path.glob("error-sink.jsonl.*"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "x" * 8
    assert "rotation probe" in sink.read_text(encoding="utf-8")


def test_sink_prunes_beyond_max_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "error-sink.jsonl"
    monkeypatch.setattr(sink_module, "SINK_MAX_ARCHIVES", 2)
    for index in range(3):
        (tmp_path / f"error-sink.jsonl.2026010{index}T000000Z").write_text(
            f"archive-{index}", encoding="utf-8"
        )
    event = make_event(
        message="prune probe",
        source_site="tests:sink:prune",
        kind="engine",
    )
    sink_module._prune_sink_archives(sink)
    archives = sorted(tmp_path.glob("error-sink.jsonl.*"))
    assert len(archives) == 2


def test_sink_rate_limit_emits_suppression_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "error-sink.jsonl"
    monkeypatch.setattr(sink_module, "SINK_EVENTS_PER_MINUTE", 2)
    minutes = iter(["202601011200"] * 5 + ["202601011201"])
    monkeypatch.setattr(sink_module, "_utc_minute", lambda: next(minutes))
    for index in range(6):
        sink_module.append_sink(
            make_event(
                message=f"event-{index}",
                source_site=f"tests:sink:rate:{index}",
                kind="engine",
            ),
            sink,
        )
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["message"] == "event-0"
    assert rows[1]["message"] == "event-1"
    summary = next(row for row in rows if row["source_site"] == "telemetry:sink:rate-limit")
    assert "3 events suppressed" in summary["message"]


def test_packaged_build_defaults_on_with_a_dsn_and_a_checkout_off() -> None:
    """OBS-04 (Mon 21 Sep 2026): a packaged build with a DSN reports; the
    consent gate (OBS-05) holds every send until the tester accepts, and a
    checkout stays off because a DSN in a developer's .env is not consent.
    """
    ship = decide_telemetry({DSN_ENV: DSN}, build_source="payload", release=SHA)
    assert ship.enabled is True
    assert ship.explicit is False, "default-on is not an operator ask; consent applies"
    assert ship.environment == "ship"

    dev = decide_telemetry({DSN_ENV: DSN}, build_source="repo", release=SHA)
    assert dev.enabled is False

    fleet = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    assert fleet.enabled is True
    assert fleet.release == SHA
