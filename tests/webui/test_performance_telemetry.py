"""Real HTTP/filesystem coverage for bounded performance telemetry.

The log directories are injected by writing ``app.state`` after construction
rather than through ``create_app`` keyword arguments. ``app.py`` is a hotspot
file that several parallel branches edit, so this port touches exactly two
lines of it (the import and the ``include_router``); the route already reads
both directories through ``getattr(request.app.state, ...)`` with its own
module-level default, so nothing here is a hidden fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared import private_files
from apps.shared.machine_pressure import read_machine_pressure, valid_kernel_pressure_level
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


def _app(
    *,
    performance_log_dir: Path | None = None,
    performance_process_log_dirs: tuple[Path, ...] | None = None,
) -> FastAPI:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
    )
    if performance_log_dir is not None:
        app.state.performance_log_dir = performance_log_dir
    if performance_process_log_dirs is not None:
        app.state.performance_process_log_dirs = performance_process_log_dirs
    return app


def _captured_client_sample() -> dict[str, object]:
    """Sanitized shape captured from the real /performance read model."""

    return {
        "client_sample_id": "client-sample-air-001",
        "client_session_id": "desktop-shell-air-001",
        "client_timestamp": "2026-08-21T20:08:05.220Z",
        "route": "/performance",
        "page_uptime_ms": 9_248_000,
        "js_heap_mb": None,
        "pcm_estimated_mb": 250,
        "anlz_estimated_mb": 19,
        "anlz_entry_count": 6,
        "prefetch_mb": 21,
        "prefetch_count": 3,
        "audio_health_hz": None,
        "audio_health_level": "idle",
        "perf_event_count": 20,
        "decks": [
            {
                "deck_id": deck_id,
                "stable_id": f"captured-stable-id-{deck_id}",
                "duration_ms": 240_000,
                "playing": False,
                "audible": False,
                "transport_pending": False,
                "stem_status": "unavailable",
                "last_load_latency_ms": 569 if deck_id == 2 else None,
                "sync_error": None,
                "processor_error": None,
            }
            for deck_id in (1, 2, 3, 4)
        ],
    }


def _captured_process_record() -> dict[str, object]:
    """Sanitized subset of the Air record written by the native probe."""

    return {
        "schema_version": 1,
        "kind": "sample",
        "timestamp": "2026-08-21T20:08:05.220Z",
        "totals": {
            "physical_footprint_mb": 1677.7,
            "summed_lifetime_peak_mb": 2224.9,
            "cpu_percent": 1.28,
            "process_count": 5,
        },
        "processes": [
            {"role": role, "physical_footprint_mb": mb, "command": "private path"}
            for role, mb in (
                ("desktop-shell", 31.9),
                ("python-engine", 151.4),
                ("webkit-gpu", 98.1),
                ("webkit-networking", 13.4),
                ("webkit-webcontent", 1382.9),
            )
        ],
    }


def test_client_sample_get_returns_404_before_any_post(tmp_path: Path) -> None:
    with TestClient(_app(performance_log_dir=tmp_path)) as client:
        response = client.get("/api/v1/performance/telemetry/client-samples")

    assert response.status_code == 404


def test_client_sample_get_returns_last_post(tmp_path: Path) -> None:
    payload = _captured_client_sample()
    payload["audio_health_level"] = "warn"
    with TestClient(_app(performance_log_dir=tmp_path)) as client:
        posted = client.post("/api/v1/performance/telemetry/client-samples", json=payload)
        response = client.get("/api/v1/performance/telemetry/client-samples")

    assert posted.status_code == 202
    assert response.status_code == 200
    body = response.json()
    assert body["audio_health_level"] == "warn"
    assert body["event_id"] == posted.json()["event_id"]


def test_bind_feature_state_defaults_performance_log_dir_to_client_dir(
    tmp_path: Path,
) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/performance/telemetry/client-samples",
            json=_captured_client_sample(),
        )

    assert response.status_code == 202
    assert list(tmp_path.glob("webui-performance-*.log"))


def test_client_sample_writes_private_bounded_record(tmp_path: Path) -> None:
    with TestClient(_app(performance_log_dir=tmp_path)) as client:
        response = client.post(
            "/api/v1/performance/telemetry/client-samples",
            json=_captured_client_sample(),
        )

    assert response.status_code == 202
    assert response.json()["stored"] is True
    paths = list(tmp_path.glob("webui-performance-*.log"))
    assert len(paths) == 1
    assert private_files.is_owner_only(paths[0])
    record = json.loads(paths[0].read_text(encoding="utf-8"))
    assert record["kind"] == "client-performance-sample"
    assert record["pcm_estimated_mb"] == 250
    assert record["decks"][1]["last_load_latency_ms"] == 569
    assert record["received_at"].endswith("Z")


def test_client_sample_refuses_payloads_outside_the_declared_bounds(tmp_path: Path) -> None:
    """Every bound the model declares must reject, and reject before writing."""

    wrong_deck_count = _captured_client_sample()
    wrong_deck_count["decks"] = wrong_deck_count["decks"][:3]  # type: ignore[index]

    negative_memory = _captured_client_sample()
    negative_memory["pcm_estimated_mb"] = -1

    unknown_health_level = _captured_client_sample()
    unknown_health_level["audio_health_level"] = "catastrophic"

    oversized_error = _captured_client_sample()
    oversized_error["decks"][0]["sync_error"] = "x" * 2049  # type: ignore[index]

    with TestClient(_app(performance_log_dir=tmp_path)) as client:
        for payload in (
            wrong_deck_count,
            negative_memory,
            unknown_health_level,
            oversized_error,
        ):
            response = client.post(
                "/api/v1/performance/telemetry/client-samples", json=payload
            )
            assert response.status_code == 422, payload

    assert list(tmp_path.iterdir()) == []


def test_process_endpoint_reduces_real_probe_record_to_roles(tmp_path: Path) -> None:
    process_log = tmp_path / "opendj-performance-2026-08-21.jsonl"
    process_log.write_text(
        json.dumps(_captured_process_record()) + "\n", encoding="utf-8"
    )
    with TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client:
        response = client.get("/api/v1/performance/telemetry/processes")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["totals"]["physical_footprint_mb"] == 1677.7
    assert body["by_role_mb"]["webkit-webcontent"] == 1382.9
    assert "command" not in json.dumps(body)
    assert body["stale"] is True


def test_process_endpoint_reads_the_last_sample_past_probe_error_lines(
    tmp_path: Path,
) -> None:
    """A KeepAlive probe interleaves error records; the read must skip them."""

    older = _captured_process_record()
    older["timestamp"] = "2026-08-21T19:00:00.000Z"
    older["totals"] = {**older["totals"], "physical_footprint_mb": 900.0}  # type: ignore[dict-item]
    newer = _captured_process_record()
    lines = [
        json.dumps(older),
        json.dumps(newer),
        json.dumps({"schema_version": 1, "kind": "probe-error", "error": "boom"}),
        "{ not json at all",
    ]
    (tmp_path / "opendj-performance-2026-08-21.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    with TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client:
        response = client.get("/api/v1/performance/telemetry/processes")

    body = response.json()
    assert body["available"] is True
    assert body["totals"]["physical_footprint_mb"] == 1677.7


def test_process_endpoint_is_explicit_when_native_probe_is_absent(tmp_path: Path) -> None:
    with (
        patch(
            "apps.webui.server.routes.performance_telemetry.live_process_family_state",
            return_value=([], set()),
        ),
        TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client,
    ):
        response = client.get("/api/v1/performance/telemetry/processes")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "reason": (
            "no live opendj-* processes and native process probe has not written a sample"
        ),
    }


@pytest.mark.requirement("PERFMODE-05")
def test_process_endpoint_labels_members_by_opendj_name(tmp_path: Path) -> None:
    """[if] a probe log lists named opendj [then] endpoint exposes opendj-desktop, [else stop]."""
    record = _captured_process_record()
    record["processes"] = [
        {
            "pid": 10,
            "role": "desktop-shell",
            "physical_footprint_mb": 31.9,
            "command": "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop",
        },
        {
            "pid": 11,
            "role": "python-engine",
            "physical_footprint_mb": 151.4,
            "command": "opendj-engine --name opendj-engine --port 8585",
        },
    ]
    (tmp_path / "opendj-performance-2026-08-21.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    with (
        patch(
            "apps.webui.server.routes.performance_telemetry.live_process_family_state",
            return_value=([], set()),
        ),
        TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client,
    ):
        body = client.get("/api/v1/performance/telemetry/processes").json()

    names = {member["name"] for member in body["members"]}
    assert "opendj-desktop" in names
    assert "opendj-engine" in names


def _pid_not_running() -> int:
    live = set(psutil.pids())
    pid = next(candidate for candidate in range(99_000, 1, -1) if candidate not in live)
    assert not psutil.pid_exists(pid), pid
    return pid


@pytest.mark.requirement("PERFMODE-14")
def test_process_endpoint_tags_each_member_with_its_source(tmp_path: Path) -> None:
    """[if] live and probe-log members merge [then] each names its source, [else stop]."""
    # No patching: the live walk is rooted at this test process (os.getpid()),
    # so it is a real live member; the probe record names a pid that is not
    # running, so the route must merge it and tag it probe_log.
    record = _captured_process_record()
    record["processes"] = [
        {"pid": _pid_not_running(), "role": "desktop-shell", "physical_footprint_mb": 2.7,
         "command": "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"},
    ]
    (tmp_path / "opendj-performance-2026-08-21.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    with TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client:
        members = client.get("/api/v1/performance/telemetry/processes").json()["members"]

    live = [m for m in members if m["source"] == "live"]
    probe_log = [m for m in members if m["source"] == "probe_log"]
    assert {m["source"] for m in members} == {"live", "probe_log"}
    # Live members come from the psutil walk, never from the probe record.
    assert live and not any("role" in m or "physical_footprint_mb" in m for m in live)
    assert probe_log == [
        {"name": "opendj-desktop", "source": "probe_log", "physical_footprint_mb": 2.7,
         "role": "desktop-shell"}
    ]


@pytest.mark.requirement("PERFMODE-05")
def test_process_endpoint_lists_unnamed_members(tmp_path: Path) -> None:
    """[if] a probe log includes a WebKit helper without [then] endpoint lists it, [else stop]."""
    record = _captured_process_record()
    record["processes"] = [
        {
            "pid": 20,
            "role": None,
            "physical_footprint_mb": 99.0,
            "command": (
                "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
                "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"
            ),
        }
    ]
    (tmp_path / "opendj-performance-2026-08-21.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    with (
        patch(
            "apps.webui.server.routes.performance_telemetry.live_process_family_state",
            return_value=([], set()),
        ),
        TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client,
    ):
        body = client.get("/api/v1/performance/telemetry/processes").json()

    assert any(member["name"] == "unnamed" for member in body["members"])


@pytest.mark.requirement("PERFMODE-05")
def test_process_endpoint_omits_unread_kernel_pressure(tmp_path: Path) -> None:
    """[if] live kernel pressure is unread [then] endpoint omits it, else reports it, [else stop].

    The route overlays the LIVE production reading, not the probe record, so the
    expectation comes from that same sampler: unread (Linux, or a failed read)
    must be absent, a readable level (macOS) must be reported as read. Sampled
    before and after the request because the shared sample refreshes on a TTL.
    """
    (tmp_path / "opendj-performance-2026-08-21.jsonl").write_text(
        json.dumps(_captured_process_record()) + "\n", encoding="utf-8"
    )

    def _live_level() -> int | None:
        reading = read_machine_pressure().get("kernel_memory_pressure_level")
        return valid_kernel_pressure_level(reading)

    before = _live_level()
    with TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client:
        body = client.get("/api/v1/performance/telemetry/processes").json()
    after = _live_level()

    reported = body.get("kernel_memory_pressure_level", "absent")
    expected = {"absent" if level is None else level for level in (before, after)}
    assert reported in expected, f"endpoint reported {reported!r}; live sampler read {expected}"


@pytest.mark.requirement("PERFMODE-05")
def test_process_endpoint_never_zero_fills_kernel_pressure(tmp_path: Path) -> None:
    """[if] endpoint has no readable kernel pressure [then] it never returns zero, [else stop]."""
    record = _captured_process_record()
    (tmp_path / "opendj-performance-2026-08-21.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    with TestClient(_app(performance_process_log_dirs=(tmp_path,))) as client:
        body = client.get("/api/v1/performance/telemetry/processes").json()

    assert body.get("kernel_memory_pressure_level") != 0


@pytest.mark.requirement("PERFMODE-05")
def test_client_sample_stamps_pressure_from_the_same_write(tmp_path: Path) -> None:
    """[if] a client performance sample is accepted [then] the stored record, [else stop]."""
    with TestClient(_app(performance_log_dir=tmp_path)) as client:
        response = client.post(
            "/api/v1/performance/telemetry/client-samples",
            json=_captured_client_sample(),
        )

    assert response.status_code == 202
    record = json.loads(next(tmp_path.glob("webui-performance-*.log")).read_text(encoding="utf-8"))
    pressure = record["pressure"]
    assert isinstance(pressure, dict)
    if pressure.get("available"):
        assert "cache_age_ms" in pressure
    else:
        assert isinstance(pressure.get("reason"), str)
    assert pressure.get("kernel_memory_pressure_level") != 0

pytestmark = pytest.mark.rb_parity


def test_pressure_route_answers_over_real_http() -> None:
    """The agent-native door: GET it and get a body you can act on.

    Asserted as a disjunction for the reason the unit suite spells out: a dev
    checkout returns real numbers, a packaged app (whose payload stages
    ``apps`` and not ``scripts``) must say it cannot measure. What is NOT
    allowed is a third shape -- available with nothing in it, or unavailable
    with no reason -- or a zero standing in for an unread field.
    """

    with TestClient(_app()) as client:
        response = client.get("/api/v1/performance/telemetry/pressure")

    assert response.status_code == 200
    body = response.json()
    assert body["cache_age_ms"] >= 0
    if body["available"]:
        readings = [k for k in ("load_avg_1m", "mem_free_mb", "swap_used_mb") if k in body]
        assert readings, "available=true must carry at least one real reading"
    else:
        assert body["reason"]
        assert not {"load_avg_1m", "mem_free_mb", "swap_used_mb"} & set(body)


def test_pressure_route_does_not_leak_process_detail() -> None:
    """Same allowlist discipline as /processes: no command lines, no PIDs."""

    with TestClient(_app()) as client:
        body = client.get("/api/v1/performance/telemetry/pressure").json()

    assert set(body) <= {
        "available",
        "reason",
        "cache_age_ms",
        "load_avg_1m",
        "mem_free_mb",
        "swap_used_mb",
        "kernel_memory_pressure_level",
        "churn_score",
        "swap_rate",
        "decomp_rate",
        "compressed_mb",
        "band",
        "sample_interval_ms",
        "sample_wall_ms",
        "sample_wall_p95_ms",
    }
