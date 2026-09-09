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
from scripts.diagnostics.probe_log_store import TREND_MINUTES
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


def test_trend_cannot_be_red_from_a_window_shorter_than_its_own_floor(
    tmp_path: Path,
) -> None:
    """A sub-minute, two-sample window reports UNKNOWN, never RED.

    Two samples 30 seconds apart with a 1 MB footprint step extrapolate to
    120 MB/hour - over the RED threshold - but the window is shorter than
    TREND_MINUTES, the module's own shortest window for saying anything about
    growth. verification.md: a tool that cannot measure reports UNKNOWN, never
    a verdict, so the steep slope stays an observation in the payload instead
    of failing the gate with a fabricated regression (issue #1404).
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:00:30Z", 300.0, 101.0),
    ]
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["window"]["duration_minutes"] < TREND_MINUTES
    assert result["verdict"] == "UNKNOWN"
    assert result["exit_code"] == 0
    assert "minutes sampled" in result["verdict_reason"]
    assert result["per_process"]["1183"]["slope_mb_per_hour"] == 120.0


def test_trend_is_red_when_the_same_slope_spans_the_full_floor(
    tmp_path: Path,
) -> None:
    """Control: the identical 120 MB/hour rate over >= TREND_MINUTES is RED.

    Without this, an implementation that made every window UNKNOWN would pass
    the floor test above while silencing real regressions. The floor must gate
    RED without making RED unreachable (issue #1404).
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:30:00Z", 300.0, 160.0),
    ]
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["window"]["duration_minutes"] >= TREND_MINUTES
    assert result["verdict"] == "RED"
    assert result["exit_code"] == 1
    assert "process slope 120.0 MB/hour" in result["verdict_reason"]


def test_trend_is_red_from_an_orphan_in_a_sub_floor_window(tmp_path: Path) -> None:
    """An observed orphan is RED even inside a sub-floor window.

    Orphan count is a direct observation, not an extrapolation over time: an
    orphan that exists, exists, whether the window is thirty seconds or thirty
    minutes. Only the slope signal needs TREND_MINUTES to be trustworthy, so
    the sufficiency floor must not suppress this (issue #1404 review, PR #1407).
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:00:30Z", 300.0, 100.0),
    ]
    rows[1]["suspected_orphan_count"] = 1
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["window"]["duration_minutes"] < TREND_MINUTES
    assert result["verdict"] == "RED"
    assert result["exit_code"] == 1
    assert "1 suspected orphan(s)" in result["verdict_reason"]


def test_trend_is_red_from_a_failed_unload_in_a_sub_floor_window(tmp_path: Path) -> None:
    """A failed unload check is RED even inside a sub-floor window.

    A retained-footprint unload failure is a directly observed comparison, not
    a rate extrapolated from the window's duration, so the sufficiency floor
    must not suppress it (issue #1404 review, PR #1407).
    """

    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:10:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:20:00Z", 700.0, 500.0),
    ]
    rows[0]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 0}
    rows[1]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 4}
    rows[2]["browser_perf_ring"] = {"available": True, "loaded_deck_count": 0}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["window"]["duration_minutes"] < TREND_MINUTES
    assert result["verdict"] == "RED"
    assert result["exit_code"] == 1
    assert "400.0 MB" in result["verdict_reason"]


def test_trend_reports_orphan_red_and_leaves_a_sub_floor_slope_uncounted(
    tmp_path: Path,
) -> None:
    """Mixed case: orphan present AND a steep slope, both in a sub-floor window.

    The verdict is RED on the orphan (a direct observation). The steep slope
    stays visible in the reason text as an observation, but explicitly marked
    as not counted toward the verdict, because the window is too short to
    trust the extrapolation on its own (issue #1404 review, PR #1407).
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:00:30Z", 300.0, 101.0),
    ]
    rows[1]["suspected_orphan_count"] = 1
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["window"]["duration_minutes"] < TREND_MINUTES
    assert result["verdict"] == "RED"
    assert result["exit_code"] == 1
    assert "1 suspected orphan(s)" in result["verdict_reason"]
    assert "observed but not counted" in result["verdict_reason"]
    assert result["per_process"]["1183"]["slope_mb_per_hour"] == 120.0


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


def _write_storage_value(home: Path, bundle_id: str, key: str, value: object) -> Path:
    """Write one key into a real WebKit LocalStorage sqlite file."""

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
            (key, json.dumps(value).encode("utf-8")),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _write_ring_at(home: Path, bundle_id: str, events: list[dict[str, object]]) -> Path:
    """Write one exact perf ring into a real WebKit LocalStorage sqlite file."""

    return _write_storage_value(home, bundle_id, "mdt.perfEventLog", events)


_PERF_BUCKET_BUDGETS = {"deck-load": 16, "transport-schedule": 16, "deck-state": 8, "other": 8}
"""Test-side mirror of perf-event-log.ts BUDGETS, so a fixture can be built by
running candidate rows through the SAME per-bucket FIFO the browser actually
enforces, instead of writing every candidate row straight into the fixture's
sqlite file as if the ring were unbounded."""


def _perf_bucket_of_kind(kind: str) -> str:
    """Test-side mirror of perf-event-log.ts `_bucketOf`."""

    if kind.startswith("deck-load"):
        return "deck-load"
    if kind.startswith("transport-schedule"):
        return "transport-schedule"
    if kind.startswith("deck-state") or kind == "deck-unload":
        return "deck-state"
    return "other"


def _apply_ring_budgets(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Test-side mirror of perf-event-log.ts `_withinBudgets`: keep the newest
    N rows per bucket, walking newest-to-oldest so eviction is oldest-first
    WITHIN a bucket while leaving the other buckets untouched, then restore
    chronological order.

    A fixture that writes candidate rows straight into sqlite bypasses this
    entirely, so a flood larger than a bucket's budget sits in the ring as
    inert padding a real ring could never hold (issue #1403 round-4 P3) -
    running it through the same budget logic the browser applies is what
    makes the fixture a real ring instead of an unbounded list.
    """

    kept: list[dict[str, object]] = []
    taken = dict.fromkeys(_PERF_BUCKET_BUDGETS, 0)
    for event in reversed(events):
        bucket = _perf_bucket_of_kind(str(event.get("kind", "")))
        if taken[bucket] >= _PERF_BUCKET_BUDGETS[bucket]:
            continue
        taken[bucket] += 1
        kept.append(event)
    return list(reversed(kept))


def _write_empty_deck_baseline(home: Path, bundle_id: str, t: str) -> Path:
    """Write the `mdt.deckState` empty-deck baseline the frontend stamps per load.

    Mirrors perf-event-log.ts recordDeckStateBaseline: the four decks were
    created empty at `t`, which is the fact the probe needs to read an idle
    deck as UNLOADED rather than as "no deck-state evidence".
    """

    return _write_storage_value(
        home,
        bundle_id,
        "mdt.deckState",
        {"v": 1, "t": t, "unloaded": [1, 2, 3, 4]},
    )


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


def test_loaded_deck_count_resolves_under_an_other_bucket_flood_when_baseline_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `other`-bucket flood cannot drive loaded_deck_count to None.

    The control issue #1403 asks for. The merged PR's ring tests exercised a
    ring holding only the new kinds, but a real ring also carries audio-context,
    sync-failure and beat-sync-skip rows in the shared 8-row `other` budget.
    Those used to evict the four empty-deck boot rows, so every deck that had
    no other surviving row dropped out of the probe's reconstruction and
    `len(deck_state)` fell below 4 - loaded_deck_count read None under ordinary
    use, indistinguishable from an app that was never exercised.

    The fix records the empty-deck baseline OUTSIDE the ring (mdt.deckState), so
    an idle deck's unloaded state cannot be evicted. Decks 2-4 below have no
    ring row at all: only the baseline says they are empty.

    The 20 sync-failure rows are run through `_apply_ring_budgets` before being
    written, the same per-bucket FIFO perf-event-log.ts applies, so only the
    newest 8 (the `other` bucket's own budget) actually reach the fixture - a
    real ring would never hold more of them beside the deck row (round-4 P3:
    writing all 20 straight into sqlite bypassed the budget logic entirely and
    left them as inert padding no eviction ever touched).
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    candidate_events = [
        {"t": "2026-09-06T11:05:00.000Z", "kind": "deck-load sid=aaaaaaaaaaaa1", "deck": 1},
        # Unrelated quiet kinds, far past the 8-row `other` budget. They must
        # not decide whether the deck-state signal survives.
        *[
            {
                "t": f"2026-09-06T11:{minute:02d}:00.000Z",
                "kind": "sync-failure",
                "deck": 1,
                "message": "could not phase lock",
            }
            for minute in range(6, 26)
        ],
    ]
    surviving_events = _apply_ring_budgets(candidate_events)
    assert len(surviving_events) == 9, "1 deck-load row + the other bucket's 8-row budget"
    _write_ring_at(tmp_path, "com.opendj.desktop", surviving_events)
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", "2026-09-06T11:00:00.000Z")

    ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)

    assert ring["available"] is True
    assert ring["loaded_deck_count"] == 1


def test_empty_baseline_reads_as_zero_loaded_not_as_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh boot with nothing loaded resolves to 0, not to None.

    None must be reserved for rings with no deck-state evidence at all; a
    present empty-deck baseline is positive evidence that all four decks are
    unloaded, which is what lets the unload check open a post-boot baseline.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _write_ring_at(
        tmp_path,
        "com.opendj.desktop",
        [
            {"t": "2026-09-06T11:06:00.000Z", "kind": "audio-context", "deck": None, "message": "x"}
            for _ in range(10)
        ],
    )
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", "2026-09-06T11:00:00.000Z")

    assert browser_perf_ring(DEFAULT_BUNDLE_IDS)["loaded_deck_count"] == 0


def test_loaded_deck_count_is_none_when_a_deck_load_row_may_have_been_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evicted deck-load row must not silently revert a deck to unloaded.

    Deck 1 loads right after the baseline, then 16 more deck-load-bucket rows
    for other decks pass through the ring - the deck-load bucket's budget - so
    deck 1's own completed-load row is exactly the kind of row that budget
    would have evicted by now. The ring below simulates that: it never shows
    deck 1's completed load at all, only the 16 later rows. Without the fix,
    `_deck_states` would keep deck 1 at the baseline's default False forever,
    fabricating a clean unload for a deck that may still be loaded. The fix
    must drop deck 1 out of the result once the bucket is at budget, which
    keeps `loaded_deck_count` at None instead of publishing a wrong 3.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", "2026-09-06T11:00:00.000Z")
    later_loads = [
        {
            "t": f"2026-09-06T11:{minute:02d}:00.000Z",
            "kind": f"deck-load sid=bbbbbbbbbb{minute:02d}",
            "deck": 2 + (minute % 3),
        }
        for minute in range(1, 17)
    ]
    _write_ring_at(tmp_path, "com.opendj.desktop", later_loads)

    ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)

    assert ring["deck_load_count_in_ring"] == 16
    assert ring["loaded_deck_count"] is None


def test_loaded_deck_count_resolves_from_baseline_when_budget_is_pre_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a returning user's PRE-gate ring rows must not zero out the baseline.

    The ring persists across page loads, so a returning user who has already
    done four 4-deck loads in an earlier session can have the deck-load bucket
    sitting at its budget of 16 rows before this session's baseline is even
    stamped. None of those rows could have evicted a post-gate row, because
    eviction is oldest-first and nothing has been appended after the gate yet.
    The eviction guard must count only post-gate deck-load-bucket rows, so this
    baseline-only session still resolves loaded_deck_count to 0 (all four decks
    unloaded per the baseline), not None.

    deck_load_count_in_ring and last_deck_load are gated the same instant as
    deck_state (round-4 P3): since every prior-session row here is PRE-gate,
    the gated fields read as empty for this session too, rather than reporting
    16 loads and a stale last_deck_load next to a 0 that belongs to a
    different window.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    prior_session_loads = [
        {
            "t": f"2026-09-06T10:{44 + minute:02d}:00.000Z",
            "kind": f"deck-load sid=aaaaaaaaaaa{minute:02d}",
            "deck": 1 + (minute % 4),
        }
        for minute in range(16)
    ]
    _write_ring_at(tmp_path, "com.opendj.desktop", prior_session_loads)
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", "2026-09-06T11:00:00.000Z")

    ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)

    assert ring["deck_load_count_in_ring"] == 0
    assert ring["last_deck_load"] is None
    assert ring["loaded_deck_count"] == 0


def test_loaded_deck_count_still_resolves_under_the_deck_load_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: below the deck-load bucket's budget, a baseline-only deck still counts.

    Without this, an implementation that always dropped unobserved baseline
    decks would pass the eviction-hazard test above while never resolving a
    deck count under ordinary, well-under-budget use.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", "2026-09-06T11:00:00.000Z")
    _write_ring_at(
        tmp_path,
        "com.opendj.desktop",
        [
            {"t": "2026-09-06T11:01:00.000Z", "kind": "deck-load sid=aaaaaaaaaaa1", "deck": 1},
            {"t": "2026-09-06T11:02:00.000Z", "kind": "deck-load sid=aaaaaaaaaaa2", "deck": 2},
        ],
    )

    ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)

    assert ring["deck_load_count_in_ring"] == 2
    assert ring["loaded_deck_count"] == 2


def test_loaded_deck_count_is_none_when_a_stale_unload_outlives_its_evicted_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 P1: a surviving unload does not prove a deck is still unloaded.

    `deck-load` (budget 16) and `deck-state` (budget 8) are separate FIFO
    buckets, so a deck's OWN newer row can be evicted from one bucket while an
    older row for the SAME deck survives in the other - eviction only orders
    rows within a bucket, never across buckets. Sequence, all post-gate:
    decks 1-4 unload (4 `deck-unload` rows, deck-state bucket), decks 1-4 load
    again (4 fresh completed loads, deck-load bucket), then 16 more deck-load
    attempts fail (`deck-load-fail`, still the deck-load bucket per
    perf-event-log.ts `_bucketOf`). The 16 failures push the deck-load bucket
    to 20 post-gate rows against its 16 budget, evicting exactly the oldest 4 -
    the fresh reloads - while the 4 stale unloads sit untouched in the
    deck-state bucket, nowhere near its 8-row budget.

    Every deck now has a post-gate row (its stale unload), so the OLD guard -
    which only dropped a deck with NO post-gate row at all - does nothing, and
    last-observation-wins reads all four as unloaded: `loaded_deck_count`
    would publish 0 while all four decks are actually loaded, exactly the
    fabricated clean unload issue #1403 exists to prevent. The fix must reject
    each deck's stale unload because the deck-load bucket, which could hold a
    newer row for that same deck, is at budget with a horizon newer than the
    unload.

    The fixture below simulates the ring AFTER that eviction already happened,
    the same convention `test_loaded_deck_count_is_none_when_a_deck_load_row_
    may_have_been_evicted` above uses: it omits the 4 fresh reloads entirely
    (they are what got evicted) and writes only what survives - the 4 stale
    unloads plus the 16 failed attempts that pushed the deck-load bucket to
    its budget.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    gate = "2026-09-06T11:00:00.000Z"
    unloads: list[dict[str, object]] = [
        {"t": f"2026-09-06T11:0{deck}:00.000Z", "kind": "deck-unload", "deck": deck}
        for deck in (1, 2, 3, 4)
    ]
    failed_attempts: list[dict[str, object]] = [
        {
            "t": f"2026-09-06T11:{9 + minute:02d}:00.000Z",
            "kind": "deck-load-fail",
            "deck": 1 + (minute % 4),
        }
        for minute in range(16)
    ]
    _write_ring_at(tmp_path, "com.opendj.desktop", [*unloads, *failed_attempts])
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", gate)

    ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)

    assert ring["loaded_deck_count"] is None
    assert ring["deck_state_reason"] == "evicted:deck1,deck2,deck3,deck4"


def test_loaded_deck_count_resolves_exactly_when_every_deck_is_observed_post_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the round-4 case above: well under both bucket budgets, a
    mix of loads and unloads across both buckets still resolves an exact count.

    Without this, an implementation that distrusted a deck whenever the OTHER
    bucket held any row at all (rather than only when that bucket is AT
    BUDGET with a newer horizon) would pass the eviction test above while
    never resolving a count under ordinary, well-under-budget use that touches
    both buckets.
    """

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _write_empty_deck_baseline(tmp_path, "com.opendj.desktop", "2026-09-06T11:00:00.000Z")
    _write_ring_at(
        tmp_path,
        "com.opendj.desktop",
        [
            {"t": "2026-09-06T11:01:00.000Z", "kind": "deck-load sid=aaaaaaaaaaa1", "deck": 1},
            {"t": "2026-09-06T11:02:00.000Z", "kind": "deck-load sid=aaaaaaaaaaa2", "deck": 2},
            {"t": "2026-09-06T11:03:00.000Z", "kind": "deck-load sid=aaaaaaaaaaa3", "deck": 3},
            {"t": "2026-09-06T11:04:00.000Z", "kind": "deck-unload", "deck": 3},
            {"t": "2026-09-06T11:05:00.000Z", "kind": "deck-load sid=aaaaaaaaaaa4", "deck": 4},
            {"t": "2026-09-06T11:06:00.000Z", "kind": "deck-unload", "deck": 4},
        ],
    )

    ring = browser_perf_ring(DEFAULT_BUNDLE_IDS)

    assert ring["loaded_deck_count"] == 2
    assert ring["deck_state_reason"] is None


def test_trend_names_a_ring_without_deck_state_evidence_as_such(
    tmp_path: Path,
) -> None:
    """No resolved deck count is a broken ring, not an idle app.

    Before the fix, loaded_deck_count None meant BOTH "the ring never carried
    deck-state evidence" and "all four decks are unloaded", and the unload
    check returned the same generic reason for the two - an operator could not
    tell a measurement that never happened from one that simply had nothing to
    flag. A window in which no sample resolves a deck count must name the ring,
    not imply the app sat idle.
    """

    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:15:00Z", 700.0, 500.0),
    ]
    for row in rows:
        row["browser_perf_ring"] = {"available": True, "loaded_deck_count": None}
    _write_trend_rows(tmp_path, rows)

    result = trend_logs(tmp_path, "2026-08-21T20:00:00Z")

    assert result["unload_check"]["available"] is False
    assert "no deck-state evidence" in result["unload_check"]["reason"]
