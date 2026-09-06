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
    trend_logs,
)
from tests.scripts.probe_sample_fixtures import captured_record as _captured_record
from tests.scripts.probe_sample_fixtures import write_trend_rows as _write_trend_rows


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
    lane_b = _write_perf_ring(tmp_path, "com.opendj.desktop.lane-b", "2026-08-21T20:10:00.000Z")

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


def test_perf_ring_recovers_the_fresh_empty_deck_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first unload cycle starts with the player's authoritative empty deck states."""

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _write_ring_at(
        tmp_path,
        "com.opendj.desktop",
        [
            {"t": f"2026-09-06T12:00:0{deck}.000Z", "kind": "deck-state-empty", "deck": deck}
            for deck in (1, 2, 3, 4)
        ],
    )

    assert browser_perf_ring(DEFAULT_BUNDLE_IDS)["loaded_deck_count"] == 0


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


def test_trend_reports_per_process_growth_and_red_unload_delta(tmp_path: Path) -> None:
    """A 30-minute four-deck unload regression names its retained footprint."""

    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:30:00Z", 700.0, 500.0),
    ]
    rows[0]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 0}
    rows[1]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 4}
    rows[2]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 0}
    rows[2]["suspected_orphan_count"] = 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["verdict"] == "RED"
    assert result["exit_code"] != 0
    assert result["per_process"]["1183"]["slope_mb_per_hour"] == 800.0
    assert result["unload_check"]["delta_mb"] == 400.0
    assert "400.0 MB" in result["verdict_reason"]


def test_trend_is_amber_when_the_window_has_no_measurable_process_slope(tmp_path: Path) -> None:
    """Thirty minutes of one-off PIDs is unavailable evidence, not a clean trend."""

    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:30:00Z", 320.0, 120.0),
    ]
    rows[1]["processes"] = [
        {"pid": 999, "role": "desktop-shell", "physical_footprint_mb": 32.0}
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["verdict"] == "AMBER"
    assert result["exit_code"] == 0
    assert result["verdict_reason"] == "no process slope is measurable"


def test_trend_refuses_a_window_with_a_malformed_jsonl_record(tmp_path: Path) -> None:
    """Corrupt evidence cannot produce a clean resource verdict."""

    _write_trend_rows(
        tmp_path,
        [_captured_record("2026-08-21T20:00:00Z", 300.0, 100.0)],
    )
    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json}\n", encoding="utf-8")

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["available"] is False
    assert result["exit_code"] != 0
    assert "malformed" in result["reason"]


def test_trend_unload_check_does_not_compare_across_app_lifetimes(tmp_path: Path) -> None:
    """A restart leaves the four-deck unload check unavailable without one full lifetime cycle."""

    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:30:00Z", 700.0, 500.0),
    ]
    rows[0]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 0}
    rows[1]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 4}
    rows[2]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 0}
    rows[2]["shell_pid"] = 999
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["unload_check"] == {
        "available": False,
        "reason": "need post-boot baseline, four loaded decks, and later all-unloaded sample",
    }


def test_trend_unload_check_requires_the_first_sample_to_be_post_boot(tmp_path: Path) -> None:
    """A zero-deck sample after an earlier load cannot become a clean baseline."""

    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    rows = [
        _captured_record("2026-08-21T19:45:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:00:00Z", 700.0, 500.0),
        _captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:30:00Z", 700.0, 500.0),
    ]
    for row, decks in zip(rows, (4, 0, 4, 0), strict=True):
        row["browser_perf_ring"] = {"available": True, "loaded_deck_count": decks}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["unload_check"] == {
        "available": False,
        "reason": "need post-boot baseline, four loaded decks, and later all-unloaded sample",
    }


def _write_ring_at(home: Path, bundle_id: str, events: list[dict[str, object]]) -> Path:
    """Write one exact perf ring into a real WebKit LocalStorage sqlite file."""

    path = (
        home
        / "Library/WebKit"
        / bundle_id
        / "WebsiteData/Default/salt/origin/LocalStorage/localstorage.sqlite3"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
        connection.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            ("mdt.perfEventLog", json.dumps(events).encode("utf-8")),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_only_a_completed_deck_load_marks_a_deck_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `deck-load`-prefixed row that is not a COMPLETION is not a load.

    audio-engine.svelte.ts emits exactly two such kinds today, `deck-load
    sid=<id>` on success and `deck-load-fail` on the catch path, so the old
    `startswith("deck-load") and != "deck-load-fail"` denylist happens to be
    correct for the ring as it stands. The `deck-load-fail` leg below is
    therefore documentation of that contract, NOT a discriminating assertion:
    it passes against the denylist too.

    The discriminating leg is the unrecognized kind. Under a denylist any third
    `deck-load-*` kind added later reads as four loaded decks with no test going
    red, which is the failure mode this locks out.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for kind in ("deck-load-fail", "deck-load-start"):
        _write_ring_at(
            tmp_path,
            "com.opendj.desktop",
            [
                {"t": f"2026-09-05T20:0{deck}:00.000Z", "kind": kind, "deck": deck}
                for deck in (1, 2, 3, 4)
            ],
        )
        ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)
        assert ring["available"] is True
        assert ring["loaded_deck_count"] is None, f"{kind} must not read as a loaded deck"


def test_completed_deck_loads_are_still_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the failure test above: a real four-deck state still reads 4.

    Without this, a probe that reported None for everything would satisfy the
    assertion above while measuring nothing at all.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    loaded: list[dict[str, object]] = [
        {
            "t": f"2026-09-05T20:0{deck}:00.000Z",
            "kind": f"deck-load sid=aaaaaaaaaaa{deck}",
            "deck": deck,
        }
        for deck in (1, 2, 3, 4)
    ]
    _write_ring_at(tmp_path, "com.opendj.desktop", loaded)
    assert browser_perf_ring(DEFAULT_BUNDLE_IDS)["loaded_deck_count"] == 4

    _write_ring_at(
        tmp_path,
        "com.opendj.desktop",
        [*loaded, {"t": "2026-09-05T20:09:00.000Z", "kind": "deck-unload", "deck": 4}],
    )
    assert browser_perf_ring(DEFAULT_BUNDLE_IDS)["loaded_deck_count"] == 3


def test_zero_deck_samples_are_excluded_from_deck_normalized_footprints(
    tmp_path: Path,
) -> None:
    """A zero-deck sample has no per-deck value; it must not be divided by one.

    The three samples below are 300 MB at zero decks, 900 MB at four decks and
    700 MB at zero decks. `max(1, decks)` published [300.0, 225.0, 700.0], so
    two raw whole-app footprints sat in a series labelled per-deck and pulled
    its mean from 225 to 408. Only the four-deck sample has a measured
    denominator, and the two omissions are counted rather than dropped
    silently.
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:30:00Z", 700.0, 500.0),
    ]
    for row, decks in zip(rows, (0, 4, 0), strict=True):
        row["browser_perf_ring"] = {"available": True, "loaded_deck_count": decks}
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["deck_load_normalized_footprint_mb"] == [225.0]
    assert result["deck_load_normalized_excluded_zero_deck_samples"] == 2


def test_deck_normalized_footprints_still_report_every_loaded_sample(
    tmp_path: Path,
) -> None:
    """Control: with a deck loaded in every sample nothing is excluded.

    Without this an implementation that emitted an empty series for everything
    would pass the exclusion test above while measuring nothing.
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 400.0, 100.0),
        _captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
    ]
    for row, decks in zip(rows, (2, 4), strict=True):
        row["browser_perf_ring"] = {"available": True, "loaded_deck_count": decks}
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["deck_load_normalized_footprint_mb"] == [200.0, 225.0]
    assert result["deck_load_normalized_excluded_zero_deck_samples"] == 0
