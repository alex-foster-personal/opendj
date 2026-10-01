"""Nightly perf KPI rows are read in capture order, not publish order.

Found by Codex (PR #4474, P2/BLOCKING, "Order backlog against rows already on
the branch"): hosts publish through one standing ledger branch, so append order
is publish order. A 03:00Z reading parked in one host's outbox can land after a
17:30Z reading another host already published, and both readers
(`kpi_readings.newest_reading`, and `GET /api/v1/bench/perf-kpi`'s
(date, round) collapse) broke same-date ties by append order, so the older
value read as current. Rows now carry `captured_at`, and both readers order
same-date rows by it.

[if] a later same-day capture is published before an older one [then] both readers still read the later capture as current, [else stop].

Every test here drives the production functions directly with real rows built
by `build_ledger_rows`: no gh replay, no monkeypatch (Codex, PR #4474,
P1/BLOCKING, "Remove the fake gh path from the ordering test").

-Claude
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from apps.webui.server.routes.bench import _in_capture_order
from scripts.perf.kpi_readings import newest_reading
from scripts.perf.perf_kpi_config import PerfKpiConfig, TrackProfile
from scripts.perf.perf_kpi_ledger_pr import _publish_candidates
from scripts.perf.perf_kpi_nightly import WarmMedian, build_ledger_rows

pytestmark = pytest.mark.requirement("DEVOPS-08")

KPI = "deck_load_anlz_warm_median_ms_small_mp3"


def _config(machine: str) -> PerfKpiConfig:
    state_dir = Path("/nonexistent/perf-kpi-state")
    return PerfKpiConfig(
        ledger_path=state_dir / "kpi-ledger.json",
        state_dir=state_dir,
        health_log=state_dir / "health.jsonl",
        history_log=state_dir / "history.jsonl",
        health_state=state_dir / "health-state.json",
        preview_health_url="http://127.0.0.1:8728/api/v1/health",
        preview_engine_label="com.af.opendj-preview-engine",
        scratch_port=8699,
        samples=2,
        machine=machine,
        tracks=(TrackProfile("small_mp3", "sid-small", "small"),),
        ledger_worktree=state_dir / "ledger-worktree",
    )


def _run_rows(machine: str, now: dt.datetime, median_ms: float | None) -> list[dict[str, Any]]:
    error = None if median_ms is not None else "engine unreachable"
    return build_ledger_rows(
        _config(machine),
        git_sha="deadbeef",
        now=now,
        measurements=[WarmMedian("small_mp3", "anlz", median_ms, error, "1 track")],
    )


def _at(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 9, 29, hour, minute, 0, 125000, tzinfo=dt.UTC)


def test_nightly_rows_carry_their_run_capture_time() -> None:
    """[if] a nightly run writes rows [then] each carries the run's UTC capture
    time as fixed-width ISO 8601, so string order is time order."""
    rows = _run_rows("air", _at(3, 0), 100.0) + _run_rows("air", _at(3, 0), None)
    assert {row["captured_at"] for row in rows} == {"2026-09-29T03:00:00.125000+00:00"}
    assert {row["date"] for row in rows} == {"2026-09-29"}
    whole_second = dt.datetime(2026, 9, 29, 17, 30, tzinfo=dt.UTC)
    assert _run_rows("air", whole_second, 100.0)[0]["captured_at"] == (
        "2026-09-29T17:30:00.000000+00:00"
    )


def test_readers_prefer_the_later_capture_over_publish_order() -> None:
    """[if] another host's 17:30Z reading is already on the branch and this
    host then publishes its 03:00Z backlog and a failed run [then] both readers
    treat 17:30Z as the current reading, [else stop]."""
    branch_order = (
        _run_rows("demon-llama", _at(17, 30), 900.0)
        + _run_rows("air", _at(3, 0), 100.0)
        + _run_rows("air", _at(17, 45), None)
    )
    ordered = _in_capture_order(branch_order)
    assert [(row["machine"], row["value"]) for row in ordered] == [
        ("air", 100.0),
        ("demon-llama", 900.0),
        ("air", None),
    ]
    numeric_only = [row for row in branch_order if row["value"] is not None]
    assert newest_reading(numeric_only, KPI).value == 900.0


def test_rows_without_capture_time_keep_append_order_ahead_of_timed_rows() -> None:
    """[if] a date holds legacy rows with no `captured_at` [then] they keep
    their append order and sort before that date's timed rows, and an earlier
    date still sorts first: the overshoot control, since ordering by capture
    time alone would move rows across dates."""
    legacy = [
        {"date": "2026-09-29", "kpi": KPI, "value": 1.0, "note": "legacy-a"},
        {"date": "2026-09-29", "kpi": KPI, "value": 2.0, "note": "legacy-b"},
    ]
    timed = _run_rows("air", _at(3, 0), 100.0)
    earlier_day = [{**timed[0], "date": "2026-09-28", "captured_at": "2026-09-30T00:00:00"}]
    ordered = _in_capture_order(timed + legacy + earlier_day)
    assert [row["value"] for row in ordered] == [100.0, 1.0, 2.0, 100.0]
    assert ordered[0]["date"] == "2026-09-28"
    assert newest_reading(legacy, KPI).value == 2.0


def test_publish_candidates_put_the_outbox_backlog_first() -> None:
    """[if] tonight's publish also carries an earlier failed run's outbox
    [then] the older outbox rows come first and a row already parked there is
    not published twice."""
    outbox = _run_rows("air", _at(3, 0), 100.0)
    tonight = _run_rows("air", _at(17, 30), 120.0)
    assert _publish_candidates(tonight, outbox) == outbox + tonight
    assert _publish_candidates(outbox + tonight, outbox) == outbox + tonight
