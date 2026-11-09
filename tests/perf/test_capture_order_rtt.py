"""AGENT-10047: capture_order_rtt turns GET /commands/timings into ledger rows.
[if] under 20 orders yields a number, or the rows do not carry the endpoint's total and claim p95 [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.capture_order_rtt import (
    CLAIM_KPI,
    ROUND_TRIP_KPI,
    main,
    timings_to_ledger_rows,
)

pytestmark = pytest.mark.requirement("AGENT-10047")

REPO_ROOT = Path(__file__).resolve().parents[2]
META = CaptureMeta(capture_id="perf-capture-x", date="2026-11-09", machine="test-host", sha="abc")


def _doc(
    count: int,
    total_p95: float = 42.5,
    claim_p95: float = 7.25,
    by_type: dict | None = None,
) -> dict:
    return {
        "count": count,
        "max_orders": 200,
        "total_ms": {"p50": 1.0, "p95": total_p95},
        "claim_ms": {"p50": 0.5, "p95": claim_p95},
        "execute_ms": {"p50": 0.5, "p95": 1.0},
        "by_type": by_type or {},
    }


def test_twenty_five_orders_measured_rows() -> None:
    rows = timings_to_ledger_rows(_doc(25), META)
    assert len(rows) == 2
    assert rows[0]["kpi"] == ROUND_TRIP_KPI
    assert rows[0]["value"] == 42.5
    assert rows[1]["kpi"] == CLAIM_KPI
    assert rows[1]["value"] == 7.25
    assert rows[0]["unit"] == "ms"
    assert "status" not in rows[0]
    assert "n=25" in rows[0]["note"]


def test_nineteen_orders_withheld() -> None:
    rows = timings_to_ledger_rows(_doc(19), META)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "withheld"
        assert row["value"] is None
        assert row["measured"] is False
        assert "19 of 20" in row["note"]


def test_twenty_orders_boundary_measured() -> None:
    rows = timings_to_ledger_rows(_doc(20), META)
    assert rows[0]["value"] == 42.5
    assert "status" not in rows[0]


def test_by_type_in_note() -> None:
    doc = _doc(25, by_type={"play": {"count": 2, "total_ms": {"p50": 1.0, "p95": 12.3}}})
    note = timings_to_ledger_rows(doc, META)[0]["note"]
    assert "play=" in note


def test_invalid_count_or_p95_raises() -> None:
    with pytest.raises(ValueError):
        timings_to_ledger_rows({"count": -1, "total_ms": {}, "claim_ms": {}}, META)
    with pytest.raises(ValueError):
        timings_to_ledger_rows(
            {"count": 25, "total_ms": {"p95": float("nan")}, "claim_ms": {"p95": 1.0}},
            META,
        )


def test_cli_dry_run_from_file(tmp_path: Path) -> None:
    path = tmp_path / "timings.json"
    path.write_text(json.dumps(_doc(25)), encoding="utf-8")
    ledger = tmp_path / "ledger.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.perf.capture_order_rtt",
            "--timings",
            str(path),
            "--dry-run",
            "--ledger",
            str(ledger),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "agent_order_round_trip_ms_p95  42.5 ms" in proc.stdout
    assert "agent_order_claim_ms_p95  7.25 ms" in proc.stdout
    assert not ledger.exists()


def test_cli_engine_monkeypatched(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_url: str, _timeout: float) -> tuple[int, dict, bytes]:
        return 200, {}, json.dumps(_doc(30)).encode()

    monkeypatch.setattr("scripts.perf.capture_order_rtt.fetch_url", _fake)
    assert main(["--engine", "http://127.0.0.1:9", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ROUND_TRIP_KPI in out and CLAIM_KPI in out

    def _503(_url: str, _timeout: float) -> tuple[int, dict, bytes]:
        return 503, {}, b""

    monkeypatch.setattr("scripts.perf.capture_order_rtt.fetch_url", _503)
    assert main(["--engine", "http://127.0.0.1:9", "--dry-run"]) == 2
    assert "503" in capsys.readouterr().err
