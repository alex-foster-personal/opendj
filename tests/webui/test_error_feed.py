"""Hermetic coverage for GET /api/v1/errors across diagnostic sinks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


def _app(
    tmp_path: Path,
    *,
    legacy_dir: Path | None = None,
) -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    if legacy_dir is not None:
        app.state.client_error_legacy_log_dir = legacy_dir
    return TestClient(app)


def _write_client_error(
    log_dir: Path,
    *,
    event_id: str,
    received_at: str,
    message: str = "boom",
    stack: str | None = None,
    url: str | None = None,
    context: dict[str, object] | None = None,
) -> None:
    day = received_at[:10]
    path = log_dir / f"webui-client-errors-{day}.log"
    record = {
        "event_id": event_id,
        "received_at": received_at,
        "kind": "ui-error",
        "message": message,
        "stack": stack,
        "url": url,
        "context": context or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _captured_client_sample(**overrides: object) -> dict[str, object]:
    sample = {
        "client_sample_id": "sample-1",
        "client_session_id": "session-1",
        "client_timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "route": "/performance",
        "page_uptime_ms": 1000,
        "js_heap_mb": None,
        "pcm_estimated_mb": 0,
        "anlz_estimated_mb": 0,
        "anlz_entry_count": 0,
        "prefetch_mb": 0,
        "prefetch_count": 0,
        "audio_health_hz": None,
        "audio_health_level": "idle",
        "perf_event_count": 0,
        "decks": [
            {
                "deck_id": deck_id,
                "stable_id": None,
                "duration_ms": None,
                "playing": False,
                "audible": False,
                "transport_pending": False,
                "stem_status": "unavailable",
                "last_load_latency_ms": None,
                "sync_error": None,
                "processor_error": None,
            }
            for deck_id in (1, 2, 3, 4)
        ],
    }
    sample.update(overrides)
    return sample


def test_default_window_includes_recent_client_and_engine_errors(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    recent = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    old = (now - timedelta(hours=2)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _write_client_error(tmp_path, event_id="recent-1", received_at=recent)
    _write_client_error(tmp_path, event_id="old-1", received_at=old)
    engine_line = f"{recent[:19].replace('T', ' ')} ERROR: engine blew up\n"
    (tmp_path / "engine.log").write_text(engine_line, encoding="utf-8")

    with _app(tmp_path) as client:
        response = client.get("/api/v1/errors")

    assert response.status_code == 200
    body = response.json()
    event_ids = {event.get("event_id") for event in body["events"]}
    sources = {event["source"] for event in body["events"]}
    assert "recent-1" in event_ids
    assert "old-1" not in event_ids
    assert "client-errors" in sources
    assert "engine.log" in sources


def test_client_error_stack_and_context_are_returned(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _write_client_error(
        tmp_path,
        event_id="stack-1",
        received_at=now,
        stack="Error: boom\n    at x",
        url="http://127.0.0.1:3847/performance",
        context={"source": "perf-event"},
    )

    with _app(tmp_path) as client:
        body = client.get("/api/v1/errors").json()

    event = next(item for item in body["events"] if item.get("event_id") == "stack-1")
    assert event["stack"] == "Error: boom\n    at x"
    assert event["url"] == "http://127.0.0.1:3847/performance"
    assert event["context"] == {"source": "perf-event"}


def test_duplicate_event_id_across_roots_is_emitted_once(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _write_client_error(tmp_path, event_id="dup-1", received_at=now)
    _write_client_error(legacy, event_id="dup-1", received_at=now, message="legacy copy")

    with _app(tmp_path, legacy_dir=legacy) as client:
        body = client.get("/api/v1/errors").json()

    assert len([event for event in body["events"] if event.get("event_id") == "dup-1"]) == 1


def test_ui_mirror_unavailable_without_409(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        response = client.get("/api/v1/errors")

    assert response.status_code == 200
    mirror_sink = next(item for item in response.json()["sinks"] if item["id"] == "ui-mirror")
    assert mirror_sink["available"] is False
    assert mirror_sink["reason"] == "no page open"


def test_ui_mirror_faults_are_included(tmp_path: Path) -> None:
    fault_time = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with _app(tmp_path) as client:
        client.put(
            "/api/v1/state/ui-mirror",
            json={
                "client_open": True,
                "audio_health": {
                    "recent_faults": [
                        {
                            "t": fault_time,
                            "kind": "audio-output-dead",
                            "message": "no output",
                            "age_ms": 1000,
                        }
                    ]
                },
            },
        )
        body = client.get("/api/v1/errors").json()

    assert any(
        event["source"] == "ui-mirror" and event["kind"] == "audio-output-dead"
        for event in body["events"]
    )


def test_engine_warn_keeps_warning_not_info(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    recent = now.isoformat(timespec="milliseconds")
    old = (now - timedelta(hours=2)).isoformat(timespec="milliseconds")
    lines = [
        json.dumps(
            {
                "boot_id": "boot",
                "level": "WARNING",
                "logger": "tests",
                "message": "warn now",
                "timestamp": recent,
            }
        ),
        json.dumps(
            {
                "boot_id": "boot",
                "level": "INFO",
                "logger": "tests",
                "message": "info now",
                "timestamp": recent,
            }
        ),
        json.dumps(
            {
                "boot_id": "boot",
                "level": "WARNING",
                "logger": "tests",
                "message": "warn old",
                "timestamp": old,
            }
        ),
    ]
    (tmp_path / "engine-warn.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with _app(tmp_path) as client:
        body = client.get("/api/v1/errors").json()

    messages = [event["message"] for event in body["events"] if event["source"] == "engine-warn.log"]
    assert messages == ["warn now"]


def test_client_sample_error_appears_in_feed(tmp_path: Path) -> None:
    sample = _captured_client_sample()
    sample["decks"][1]["sync_error"] = "phase"
    with _app(tmp_path) as client:
        client.post("/api/v1/performance/telemetry/client-samples", json=sample)
        body = client.get("/api/v1/errors").json()

    assert any(event["source"] == "client-samples" for event in body["events"])


def test_clean_client_sample_does_not_appear_in_feed(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        client.post(
            "/api/v1/performance/telemetry/client-samples",
            json=_captured_client_sample(),
        )
        body = client.get("/api/v1/errors").json()

    assert not any(event["source"] == "client-samples" for event in body["events"])


def test_malformed_jsonl_line_is_skipped(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    day = now[:10]
    path = tmp_path / f"webui-client-errors-{day}.log"
    path.write_text(
        "not json\n"
        + json.dumps(
            {
                "event_id": "good-1",
                "received_at": now,
                "kind": "ui-error",
                "message": "ok",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with _app(tmp_path) as client:
        response = client.get("/api/v1/errors")

    assert response.status_code == 200
    assert any(event.get("event_id") == "good-1" for event in response.json()["events"])
    sink = next(item for item in response.json()["sinks"] if item["id"] == "client-errors")
    assert sink["unreadable_lines"] >= 1


def test_window_longer_than_seven_days_is_rejected(tmp_path: Path) -> None:
    since = (datetime.now(UTC) - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    with _app(tmp_path) as client:
        response = client.get("/api/v1/errors", params={"since": since})

    assert response.status_code == 400


pytestmark = pytest.mark.rb_parity
