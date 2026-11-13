"""A sink batch larger than the sink's per-minute limit still lands every row.

The sink admits SINK_EVENTS_PER_MINUTE rows a minute per process and drops the rest silently, so the
batch paces itself: after each full minute's worth it waits for the next minute. On Fri 13 Nov 2026 a
pass posted 171 rows in one minute and 51 (171 - 120) were missing on read-back, failing every pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.telemetry import sink as telemetry_sink
from scripts import post_build_failure


def _record(n: int) -> dict[str, object]:
    return {
        "run_id": 43000000000 + n,
        "run_attempt": 1,
        "workflow": "CI",
        "conclusion": "failure",
        "url": f"https://github.com/o/r/actions/runs/{43000000000 + n}",
        "head_sha": f"{n:040x}",
    }


@pytest.fixture
def minute_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    sink = tmp_path / "error-sink.jsonl"
    monkeypatch.setenv("OPENDJ_ERROR_SINK_LOG", str(sink))
    monkeypatch.setenv("OPENDJ_TELEMETRY", "0")
    minute = ["202611130653"]
    waits: list[str] = []
    monkeypatch.setattr(telemetry_sink, "_utc_minute", lambda: minute[0])

    def next_minute() -> None:
        waits.append(minute[0])
        minute[0] = str(int(minute[0]) + 1)

    monkeypatch.setattr(post_build_failure, "_wait_for_next_minute", next_minute, raising=False)
    telemetry_sink.reset_rate_state_for_tests()
    yield sink, waits
    telemetry_sink.reset_rate_state_for_tests()


def _post(tmp_path: Path, count: int) -> int:
    batch = tmp_path / "ci-sink-failures.json"
    batch.write_text(json.dumps([_record(n) for n in range(count)]), encoding="utf-8")
    return post_build_failure.post_batch(batch, "github-actions")


def _build_rows(sink: Path) -> int:
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    return sum(1 for row in rows if row.get("kind") == "build")


def test_a_batch_over_the_limit_waits_one_minute_and_lands_every_row(
    tmp_path: Path, minute_clock: tuple[Path, list[str]]
) -> None:
    sink, waits = minute_clock
    count = telemetry_sink.SINK_EVENTS_PER_MINUTE + 51
    assert _post(tmp_path, count) == 0
    assert _build_rows(sink) == count
    assert waits == ["202611130653"]


def test_a_batch_at_the_limit_never_waits(tmp_path: Path, minute_clock: tuple[Path, list[str]]) -> None:
    sink, waits = minute_clock
    assert _post(tmp_path, telemetry_sink.SINK_EVENTS_PER_MINUTE) == 0
    assert _build_rows(sink) == telemetry_sink.SINK_EVENTS_PER_MINUTE
    assert waits == []
