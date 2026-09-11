"""Unit tests for S5/S12 KPI capture ledger shape, error rows, and aggregation.

[if] an unreachable or failed probe is recorded as a number [then] fail, [else stop].
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from scripts.perf.capture_kpi_ledger import (
    CAPTURE_TAG,
    CaptureMeta,
    append_entries,
    build_row,
    error_row,
    mint_capture_id,
    required_error_rows,
    session_meta,
)
from scripts.perf.capture_kpis import main, parse_scenarios
from scripts.perf.capture_s5 import capture as capture_s5
from scripts.perf.capture_s5 import headline_ms, median_ms
from scripts.perf.capture_s12 import capture as capture_s12
from scripts.perf.kpi_readings import _as_float, _resolve_cache_state_band, newest_reading
from scripts.perf.kpi_scorecard import UNKNOWN, UNMEASURED, score_scenarios

pytestmark = pytest.mark.requirement("PERF-CAPTURE-01")

_REPO = Path(__file__).resolve().parents[2]


def _shipped_map() -> dict:
    return json.loads((_REPO / "docs" / "perf" / "kpi-map.json").read_text())


def _meta() -> CaptureMeta:
    return CaptureMeta(
        capture_id="perf-capture-20260911T151205Z",
        date="2026-09-11",
        machine="test-host",
        sha="abc123def",
    )


def _score(sid: str, entries: list[dict], today: _dt.date | None = None):
    kpi_map = {"scenarios": {sid: _shipped_map()["scenarios"][sid]}}
    (score,) = score_scenarios(kpi_map, entries, today or _dt.date(2026, 9, 11))
    return score


def test_mint_capture_id_contains_perf_capture() -> None:
    cid = mint_capture_id(_dt.datetime(2026, 9, 11, 15, 12, 5, tzinfo=_dt.UTC))
    assert cid == "perf-capture-20260911T151205Z"
    assert "perf-capture" in cid


def test_append_entries_writes_required_fields_and_keeps_prior(tmp_path: Path) -> None:
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "_doc": "keep me",
                "schema_version": 1,
                "entries": [{"kpi": "prior", "value": 1}],
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    meta = _meta()
    row = build_row(
        kpi="packaged_deck_load_total_ms",
        value=120.5,
        unit="ms",
        method=(
            "GET /api/v1/tracks/{id}/anlz?points=38400 + GET /audio "
            "(full body), n=5 median, max of 3 tracks"
        ),
        meta=meta,
        note="warm; n=5; denominator=3 tracks",
    )
    append_entries(ledger, [row])
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["_doc"] == "keep me"
    assert payload["schema_version"] == 1
    assert payload["entries"][0]["kpi"] == "prior"
    written = payload["entries"][-1]
    assert written["sha"] == "abc123def"
    assert written["machine"] == "test-host"
    assert written["method"]
    assert written["capture_id"].startswith("perf-capture")
    assert CAPTURE_TAG in written["note"]
    assert written["source"] == "scripts.perf.capture_kpis"
    assert written["round"] == "perf-capture"


def test_error_row_is_null_and_does_not_score() -> None:
    meta = _meta()
    row = error_row(
        kpi="packaged_deck_load_total_ms",
        unit="ms",
        method="GET /api/v1/tracks/{id}/anlz + /audio",
        meta=meta,
        reason="engine unreachable: connection refused",
    )
    assert row["value"] is None
    assert row["status"] == "error"
    assert row["measured"] is False
    assert "engine unreachable" in row["note"]
    assert CAPTURE_TAG in row["note"]
    assert _as_float(row["value"]) is None
    reading = newest_reading([row], "packaged_deck_load_total_ms")
    assert reading.value is None
    assert not reading.measured
    score = _score("S5", [row])
    assert score.verdict != "PASS"
    assert score.verdict in {UNMEASURED, UNKNOWN}


def test_s5_warm_note_is_accepted_neither_or_both_rejected() -> None:
    req = _shipped_map()["scenarios"]["S5"]["required"][0]
    warm = _resolve_cache_state_band(
        req,
        "capture=perf-capture; warm; n=5; points=38400; anlz_cache=hit",
    )
    assert warm is not None
    assert warm["budget"] == 2000
    assert _resolve_cache_state_band(req, "capture=perf-capture; n=5") is None
    assert _resolve_cache_state_band(req, "capture=perf-capture; warm cold") is None


def test_s12_pair_sharing_capture_id_scores_first_only_unknown() -> None:
    meta = _meta()
    first = build_row(
        kpi="cloudsync_first_sync_s",
        value=8.5,
        unit="s",
        method="apps.sync_hub.sync_timing.PhaseTimer via run_sync HttpTransport",
        meta=meta,
        note="kind=first; denominator=12 tracks; sha=abc123def",
    )
    noop = build_row(
        kpi="cloudsync_noop_sync_s",
        value=0.87,
        unit="s",
        method="apps.sync_hub.sync_timing.PhaseTimer via run_sync HttpTransport",
        meta=meta,
        note="kind=noop; denominator=12 tracks; sha=abc123def",
    )
    assert _score("S12", [first]).verdict == UNKNOWN
    assert _score("S12", [first, noop]).verdict == "PASS"


def test_headline_is_max_of_per_track_medians() -> None:
    tracks = [
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [10.0, 20.0, 30.0, 40.0, 50.0],
        [2.0, 2.0, 2.0, 2.0, 2.0],
    ]
    assert median_ms(tracks[0]) == 3.0
    assert median_ms(tracks[1]) == 30.0
    assert headline_ms(tracks) == 30.0


def test_required_error_rows_cover_s5_and_s12() -> None:
    rows = required_error_rows(["S5", "S12"], _meta(), "engine unreachable: test")
    names = {row["kpi"] for row in rows}
    assert names == {
        "packaged_deck_load_total_ms",
        "cloudsync_first_sync_s",
        "cloudsync_noop_sync_s",
    }
    assert all(row["value"] is None for row in rows)


def test_unknown_scenario_writes_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "kpi-ledger.json"
    code = main(
        [
            "--engine",
            "http://127.0.0.1:1",
            "--scenario",
            "S99",
            "--ledger",
            str(ledger),
        ]
    )
    assert code == 2
    assert not ledger.exists()
    assert parse_scenarios("S5,S12") == ["S5", "S12"]
    assert parse_scenarios("S5") == ["S5"]


def test_session_meta_date_is_iso() -> None:
    meta = session_meta(sha="deadbeef", now=_dt.datetime(2026, 9, 11, tzinfo=_dt.UTC))
    assert meta.date == "2026-09-11"
    assert meta.capture_id.startswith("perf-capture-")


def test_empty_cfg_and_missing_hub_are_error_rows() -> None:
    meta = _meta()
    s5 = capture_s5(
        engine="http://127.0.0.1:1",
        meta=meta,
        tracks={"small": "", "large": "", "stemmed": ""},
        data_dir=None,
    )
    assert s5[0]["value"] is None
    assert s5[0]["status"] == "error"
    assert "CFG track small is empty" in s5[0]["note"]
    s12 = capture_s12(
        hub_url=None, meta=meta, data_dir=Path("/tmp"), track_count=1
    )
    assert all(row["value"] is None for row in s12)
    assert all(row["status"] == "error" for row in s12)
    assert "missing hub" in s12[0]["note"].lower() or "--hub" in s12[0]["note"]
