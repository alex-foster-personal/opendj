"""Nightly warm median and ceiling integration for perf KPI job."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts.perf import perf_kpi_nightly
from scripts.perf.perf_kpi_config import PerfKpiConfig, TrackProfile
from scripts.perf.perf_kpi_nightly import (
    WarmMedian,
    find_ceiling_breaches,
    run_nightly,
    warm_median_ms,
)


def test_warm_median_uses_samples_after_first() -> None:
    """If the first sample is cold then the warm median ignores it."""
    samples = [(200, 2000.0), (200, 80.0), (200, 90.0), (200, 85.0), (200, 88.0)]
    median, error = warm_median_ms(samples)
    assert error is None
    assert median == 86.5


def test_warm_median_errors_without_2xx() -> None:
    """If warm samples are all failures then no numeric median is produced."""
    median, error = warm_median_ms([(200, 10.0), (500, 1.0), (500, 2.0)])
    assert median is None
    assert error


def test_run_nightly_appends_and_flags_ceiling(tmp_path: Path) -> None:
    """If warm anlz exceeds 3x trailing median then nightly exits non-zero and logs breach."""
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "date": "2026-09-08",
                        "kpi": "deck_load_anlz_warm_median_ms_small_mp3",
                        "value": 50.0,
                        "unit": "ms",
                        "measured": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    config = PerfKpiConfig(
        ledger_path=ledger,
        state_dir=state_dir,
        health_log=state_dir / "health.jsonl",
        history_log=state_dir / "history.jsonl",
        health_state=state_dir / "health-state.json",
        preview_health_url="http://127.0.0.1:8728/api/v1/health",
        preview_engine_label="com.af.opendj-preview-engine",
        scratch_port=8699,
        samples=3,
        machine="air",
        tracks=(TrackProfile("small_mp3", "sid-small", "small"),),
        ledger_worktree=state_dir / "ledger-worktree",
    )

    def probe(_base: str, path: str) -> tuple[int, float]:
        if path.endswith("/anlz"):
            return 200, 200.0
        return 200, 150.0

    outcome = run_nightly(
        config,
        base_url="http://127.0.0.1:8699",
        git_sha="deadbeef",
        probe=probe,
        now=dt.datetime(2026, 9, 11, 3, 0, 5, tzinfo=dt.UTC),
        file_issue=False,
    )
    assert outcome.exit_code == 3
    assert outcome.breaches
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    nightly_rows = [
        row
        for row in doc["entries"]
        if row.get("source") == "scripts/perf/perf_kpi_job.py nightly"
        and row.get("git_sha") == "deadbeef"
    ]
    assert nightly_rows
    assert any(row["kpi"] == "deck_load_anlz_warm_median_ms_small_mp3" for row in nightly_rows)
    history = state_dir / "history.jsonl"
    assert history.exists()
    assert "ceiling_breach" in history.read_text(encoding="utf-8")


def test_run_nightly_unknown_ceiling_without_history(tmp_path: Path) -> None:
    """If there is no trailing median then nightly exits zero and logs ceiling_unknown."""
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps({"schema_version": 2, "entries": []}),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    config = PerfKpiConfig(
        ledger_path=ledger,
        state_dir=state_dir,
        health_log=state_dir / "health.jsonl",
        history_log=state_dir / "history.jsonl",
        health_state=state_dir / "health-state.json",
        preview_health_url="http://127.0.0.1:8728/api/v1/health",
        preview_engine_label="com.af.opendj-preview-engine",
        scratch_port=8699,
        samples=3,
        machine="air",
        tracks=(TrackProfile("small_mp3", "sid-small", "small"),),
        ledger_worktree=state_dir / "ledger-worktree",
    )

    def probe(_base: str, path: str) -> tuple[int, float]:
        if path.endswith("/anlz"):
            return 200, 80.0
        return 200, 50.0

    outcome = run_nightly(
        config,
        base_url="http://127.0.0.1:8699",
        git_sha="deadbeef",
        probe=probe,
        now=dt.datetime(2026, 9, 11, 3, 0, 5, tzinfo=dt.UTC),
        file_issue=False,
    )
    assert outcome.exit_code == 0
    assert outcome.breaches == []
    assert outcome.unknowns
    history = (state_dir / "history.jsonl").read_text(encoding="utf-8")
    assert "ceiling_unknown" in history
    assert "no trailing 7-day median" in history


def test_find_ceiling_ignores_audio_legs() -> None:
    """If only audio is slow then the anlz ceiling check does not fire."""
    breaches = find_ceiling_breaches(
        [{"date": "2026-09-08", "kpi": "deck_load_anlz_warm_median_ms_small_mp3", "value": 50.0}],
        [WarmMedian("small_mp3", "audio", 500.0, None, "n=4")],
        today=__import__("datetime").date(2026, 9, 11),
    )
    assert breaches == []


def _single_track_config(tmp_path: Path) -> PerfKpiConfig:
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(json.dumps({"schema_version": 2, "entries": []}), encoding="utf-8")
    state_dir = tmp_path / "state"
    return PerfKpiConfig(
        ledger_path=ledger,
        state_dir=state_dir,
        health_log=state_dir / "health.jsonl",
        history_log=state_dir / "history.jsonl",
        health_state=state_dir / "health-state.json",
        preview_health_url="http://127.0.0.1:8728/api/v1/health",
        preview_engine_label="com.af.opendj-preview-engine",
        scratch_port=8699,
        samples=3,
        machine="demon-llama",
        tracks=(
            TrackProfile("small_mp3", "sid-small", "small"),
            TrackProfile("large_mp3", "sid-large", "large"),
        ),
        ledger_worktree=state_dir / "ledger-worktree",
    )


def test_two_runs_on_one_day_get_distinct_capture_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the nightly runs twice on one UTC day [then] each run's rows carry
    their own capture_id, and every row within one run shares it.

    Found by Codex (PR #4473, P2/BLOCKING, "Give each nightly run a unique
    capture ID"): the id was `perf-kpi-<date>`, so demon-llama's 03:00Z and
    17:30Z runs on Tue 29 Sep 2026 both wrote `perf-kpi-2026-09-29`, and
    `kpi_readings` treats equal capture ids as one measurement session.
    The per-run half is the overshoot control: a per-row id is unique too,
    and would break the cohort a run's own rows must share.
    """
    config = _single_track_config(tmp_path)
    monkeypatch.setattr(perf_kpi_nightly, "capture_s5_against_engine", lambda **_: [])

    def probe(_base: str, _path: str) -> tuple[int, float]:
        return 200, 40.0

    runs = [
        dt.datetime(2026, 9, 29, 3, 0, 9, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 29, 17, 30, 59, tzinfo=dt.UTC),
    ]
    run_ids = []
    for now in runs:
        outcome = run_nightly(
            config,
            base_url="http://127.0.0.1:8699",
            git_sha="deadbeef",
            probe=probe,
            now=now,
            file_issue=False,
        )
        ids = {row["capture_id"] for row in outcome.entries}
        assert len(ids) == 1, f"one run must share one capture_id, got {sorted(ids)}"
        assert {row["date"] for row in outcome.entries} == {"2026-09-29"}
        run_ids.append(ids.pop())
    assert run_ids == ["perf-kpi-20260929T030009Z", "perf-kpi-20260929T173059Z"]


def test_run_nightly_refuses_a_clock_that_is_not_utc(tmp_path: Path) -> None:
    """[if] the capture clock is naive or not UTC [then] the run refuses before
    probing, rather than stamping local time with a Z suffix."""
    config = _single_track_config(tmp_path)

    def probe(_base: str, _path: str) -> tuple[int, float]:
        raise AssertionError("must refuse before probing")

    for bad in (
        dt.datetime(2026, 9, 29, 3, 0, 9),  # noqa: DTZ001 - the naive clock is the case under test
        dt.datetime(2026, 9, 29, 4, 0, 9, tzinfo=dt.timezone(dt.timedelta(hours=1))),
    ):
        with pytest.raises(ValueError, match="UTC"):
            run_nightly(
                config,
                base_url="http://127.0.0.1:8699",
                git_sha="deadbeef",
                probe=probe,
                now=bad,
                file_issue=False,
            )
