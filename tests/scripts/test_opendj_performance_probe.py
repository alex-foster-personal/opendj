"""Portable aggregation coverage for the macOS OpenDJ process probe."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from scripts.diagnostics.opendj_performance_probe import (
    DEFAULT_BUNDLE_IDS,
    ProcessRow,
    append_bounded_jsonl,
    browser_perf_ring,
    summarize_logs,
    suspected_orphans,
)


def _captured_record(timestamp: str, total: float, webcontent: float) -> dict[str, object]:
    """Sanitized subset of the real 2026-08-21 Air probe schema."""

    return {
        "schema_version": 1,
        "kind": "sample",
        "timestamp": timestamp,
        "totals": {
            "physical_footprint_mb": total,
            "cpu_percent": 1.25,
            "process_count": 5,
        },
        "processes": [
            {"pid": 712, "role": "desktop-shell", "physical_footprint_mb": 32.0},
            {"pid": 905, "role": "python-engine", "physical_footprint_mb": 151.0},
            {"pid": 1183, "role": "webkit-webcontent", "physical_footprint_mb": webcontent},
        ],
        "machine": {"swap_used_mb": 2600.0},
        "engine": {"jobs": {"active": []}},
        "build": {
            "available": True,
            "git_sha": "146dfba0",
            "git_sha_full": "146dfba04408752e2f45a1e7ec2d206ca6384bde",
            "git_branch": "af--rebuild-agentB-from-Fable",
            "built_at_utc": "2026-08-19T16:43:03Z",
        },
    }


def test_summary_attributes_growth_to_roles_without_exposing_commands(tmp_path: Path) -> None:
    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    rows = [
        _captured_record("2026-08-21T20:00:00Z", 1600.0, 1300.0),
        _captured_record("2026-08-21T20:30:00Z", 1700.0, 1400.0),
        _captured_record("2026-08-21T21:00:00Z", 1800.0, 1500.0),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = summarize_logs(tmp_path, hours=24 * 365 * 10)

    assert result["available"] is True
    assert result["window"]["sample_count"] == 3
    assert result["physical_footprint_mb"]["delta"] == 200.0
    assert result["linear_slope_mb_per_hour"] == 200.0
    assert result["by_role_mb"]["webkit-webcontent"]["delta"] == 200.0
    assert result["unique_process_families"] == 1
    assert result["builds"][0]["git_sha"] == "146dfba0"
    assert "command" not in json.dumps(result)


def test_orphan_detection_covers_reparented_payloads_and_known_agents() -> None:
    rows = [
        ProcessRow(
            pid=10,
            ppid=1,
            pgid=10,
            command="/Applications/Open DJ (B).app/Contents/MacOS/opendj-desktop",
        ),
        ProcessRow(
            pid=11,
            ppid=10,
            pgid=11,
            command=(
                "/Applications/Open DJ (B).app/Contents/Resources/payload/runtime/bin/python3 "
                "-m apps.engine_core serve"
            ),
        ),
        ProcessRow(
            pid=12,
            ppid=1,
            pgid=12,
            command=(
                "/Applications/Open DJ (B).app/Contents/Resources/payload/runtime/bin/python3 "
                "-m apps.engine_core orphan-worker"
            ),
        ),
        ProcessRow(pid=13, ppid=1, pgid=13, command="/usr/local/bin/agent-from-opendj"),
    ]

    result = suspected_orphans(
        rows,
        selected_pids={10, 11},
        known_descendants={13: ("/usr/local/bin/agent-from-opendj", "engine-worker")},
    )

    assert [item["pid"] for item in result] == [12, 13]
    assert "not a descendant" in result[0]["reason"]
    assert "previously associated engine-worker" in result[1]["reason"]


def _write_perf_ring(home: Path, bundle_id: str, last_timestamp: str) -> Path:
    """Build a real WebKit LocalStorage sqlite holding a real perf ring."""

    path = (
        home
        / "Library/WebKit"
        / bundle_id
        / "WebsiteData/Default/salt/origin/LocalStorage/localstorage.sqlite3"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"t": "2026-08-21T20:00:00.000Z", "kind": "deck-load", "ms": 14800},
        {"t": last_timestamp, "kind": "transport-schedule", "ms": 52},
    ]
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        connection.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("mdt.perfEventLog", json.dumps(events).encode("utf-8")),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_perf_ring_is_read_from_either_bundle_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPS-08 renamed the bundle; the ring must survive on both identifiers."""

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert DEFAULT_BUNDLE_IDS == ("com.opendj.desktop", "com.opendj.desktop.lane-b")
    lane_b = _write_perf_ring(
        tmp_path, "com.opendj.desktop.lane-b", "2026-08-21T20:10:00.000Z"
    )

    only_lane_b = browser_perf_ring(DEFAULT_BUNDLE_IDS)
    assert only_lane_b["available"] is True
    assert only_lane_b["storage_path"] == str(lane_b)
    assert only_lane_b["last_kind"] == "transport-schedule"
    assert only_lane_b["deck_load_count_in_ring"] == 1

    _write_perf_ring(tmp_path, "com.opendj.desktop", "2026-08-21T20:30:00.000Z")
    newest_wins = browser_perf_ring(DEFAULT_BUNDLE_IDS)
    assert newest_wins["last_timestamp"] == "2026-08-21T20:30:00.000Z"


def test_perf_ring_is_explicit_when_no_bundle_has_written_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert browser_perf_ring(DEFAULT_BUNDLE_IDS) == {
        "available": False,
        "reason": "perf ring not found",
    }


def test_jsonl_append_is_owner_only_and_stops_at_the_cap(tmp_path: Path) -> None:
    """The longitudinal log is bounded by refusal, never by silent truncation."""

    record = {"schema_version": 1, "kind": "sample", "timestamp": "2026-08-21T20:00:00Z"}
    path, stored = append_bounded_jsonl(tmp_path, record, cap_bytes=4096)
    assert stored is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    size_after_first = path.stat().st_size

    _, refused = append_bounded_jsonl(tmp_path, record, cap_bytes=size_after_first + 1)
    assert refused is False
    assert path.stat().st_size == size_after_first
