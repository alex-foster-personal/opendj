"""The RED-trend report waits out a slow engine, once, inside a stated bound.

``report_red_trend`` used to give the engine 1.5 s per request. On a loaded
host a freshly started engine does not answer its first client-error POST that
fast: the first write to a new daily log creates the file and then, on
Windows, restricts it with two subprocesses, AFTER the row is written. Windows
gate run 36286250497 failed with exactly ``POST .../api/v1/client-errors could
not record the RED trend: TimeoutError: timed out`` where run 36281578634
passed. So the trend an operator needs most, the one from an overloaded
machine, was the one that went unrecorded, and by the time the client gave up
the engine had usually written the row already.

The engine does NOT dedupe on ``client_event_id``: two POSTs carrying the same
id are two rows. So the report is one wall-clock budget and one attempt, never
a retry. Nothing here patches ``urlopen``; every server is a real listener
(see :mod:`tests.scripts.probe_report_servers`).

Regression lines (one-line if/then, house format):
- if a reply slower than the old 1.5 s is still a failure then a loaded host
  loses its RED trend - broken
- if a slow reply produces two rows then the fix traded a lost record for a
  duplicated one - broken
- if a dead port waits out the whole budget then every RED on a stopped
  engine costs the caller the full wait - broken
- if a dead port stops exiting with the report-failure code then "RED and
  unrecorded" reads as plain RED - broken
- if an engine that never finishes its reply holds the caller past the budget
  then the bound is not a bound - broken
- if the give-up message does not say how long it waited and how many
  attempts it made then the operator cannot tell a slow engine from a dead
  one - broken
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from scripts.diagnostics.opendj_performance_probe import (
    TREND_REPORT_FAILURE_EXIT,
    TrendReportError,
    main,
    report_red_trend,
)
from scripts.diagnostics.probe_trend_report import (
    TREND_REPORT_ATTEMPTS,
    TREND_REPORT_BUDGET_SECONDS,
)
from tests.scripts.probe_report_servers import (
    CLIENT_ERRORS_PATH,
    HEALTH_PATH,
    STALL_HEALTH_SILENT,
    STALL_MODES,
    ReplyDelayingProxy,
    StallingEngine,
    persisted_client_error_rows,
    serving_engine,
    stop_engine,
)
from tests.scripts.probe_sample_fixtures import captured_record, write_trend_rows

#: The timeout this change replaces. A held reply must outlast it, or the
#: slow-reply tests pass against the old code and prove nothing.
REPLACED_TIMEOUT_SECONDS: float = 1.5
SLOW_REPLY_SECONDS: float = 2.0

#: A budget short enough to wait out in a test.
SHORT_BUDGET_SECONDS: float = 1.0
#: Scheduling slack on a loaded host between the deadline and the raise.
BUDGET_OVERRUN_TOLERANCE_SECONDS: float = 3.0
#: The stalling server's own ceiling. Past the budget plus its tolerance, so a
#: probe with no bound returns late enough to fail the assertion below rather
#: than hang the suite.
STALL_HOLD_SECONDS: float = 8.0

#: "Fast" for a dead port. Windows retries a refused loopback connect for
#: about two seconds before reporting it, so this is not sub-second.
DEAD_PORT_CEILING_SECONDS: float = 10.0

RED_TREND: dict[str, object] = {"verdict_reason": "1 suspected orphan(s)"}


def _closed_local_port() -> int:
    """A real port with nothing listening, so a connection is really refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.parametrize(
    ("slow_method", "slow_path"),
    [("POST", CLIENT_ERRORS_PATH), ("GET", HEALTH_PATH)],
    ids=["slow-post-reply", "slow-health-reply"],
)
def test_a_reply_slower_than_the_old_timeout_still_records_exactly_one_row(
    tmp_path: Path, slow_method: str, slow_path: str
) -> None:
    """A real engine, a real held reply, and exactly one durable row."""
    assert SLOW_REPLY_SECONDS > REPLACED_TIMEOUT_SECONDS
    server, thread, engine_port = serving_engine(tmp_path)
    proxy = ReplyDelayingProxy(
        engine_port,
        slow_method=slow_method,
        slow_path=slow_path,
        delay_seconds=SLOW_REPLY_SECONDS,
    )
    try:
        started = time.monotonic()
        receipt = report_red_trend(proxy.port, RED_TREND)
        elapsed = time.monotonic() - started
    finally:
        proxy.close()
        stop_engine(server, thread)

    assert elapsed >= SLOW_REPLY_SECONDS, (
        "this test only bites if the reply really was held past the old "
        f"timeout; it came back in {elapsed:.2f} s"
    )
    assert receipt["posted"] is True
    assert receipt["http_status"] == 202
    rows = persisted_client_error_rows(tmp_path)
    assert len(rows) == 1, (
        f"if a slow reply leaves {len(rows)} rows then the report was sent more "
        "than once and the engine, which does not dedupe on client_event_id, "
        f"recorded each one - broken: {[row['event_id'] for row in rows]}"
    )
    assert rows[0]["client_event_id"] == receipt["client_event_id"]
    assert rows[0]["event_id"] == receipt["engine_event_id"]
    assert proxy.count("POST", CLIENT_ERRORS_PATH) == 1, (
        "if more than one POST reached the engine then a retry happened, "
        "whatever the row count says"
    )


def test_control_the_engine_records_one_row_per_post_so_a_retry_would_duplicate(
    tmp_path: Path,
) -> None:
    """CONTROL for the one-row assertion above: it CAN read two.

    The same trend reported twice through the same real engine is two rows.
    That is what makes "exactly one row" evidence that one POST was sent,
    rather than a count the engine would have collapsed to one anyway.
    """
    server, thread, engine_port = serving_engine(tmp_path)
    try:
        report_red_trend(engine_port, RED_TREND)
        report_red_trend(engine_port, RED_TREND)
    finally:
        stop_engine(server, thread)

    assert len(persisted_client_error_rows(tmp_path)) == 2


def _red_rows_with_engine_port(port: int) -> list[dict[str, object]]:
    rows = [
        captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
        captured_record("2026-08-21T20:30:00Z", 700.0, 500.0),
    ]
    for row, decks in zip(rows, (0, 4, 0), strict=True):
        row["browser_perf_ring"] = {"available": True, "loaded_deck_count": decks}
    rows[-1]["engine"] = {"jobs": {"active": []}, "port": port}
    return rows


def test_a_dead_port_fails_fast_with_the_report_failure_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A longer budget must not turn a refused connection into a long wait."""
    assert DEAD_PORT_CEILING_SECONDS < TREND_REPORT_BUDGET_SECONDS, (
        "the ceiling has to sit under the budget, or 'fast' means nothing"
    )
    port = _closed_local_port()
    write_trend_rows(tmp_path, _red_rows_with_engine_port(port))

    started = time.monotonic()
    code = main(["trend", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])
    elapsed = time.monotonic() - started

    printed = json.loads(capsys.readouterr().out)
    assert code == TREND_REPORT_FAILURE_EXIT
    assert printed["verdict"] == "RED"
    assert printed["client_error_posted"]["posted"] is False
    assert f"127.0.0.1:{port}" in printed["client_error_posted"]["error"]
    assert elapsed < DEAD_PORT_CEILING_SECONDS, (
        f"if a dead port takes {elapsed:.1f} s then it is waiting out the "
        f"{TREND_REPORT_BUDGET_SECONDS} s budget instead of failing on the "
        "refused connection - broken"
    )


@pytest.mark.parametrize("stall_mode", STALL_MODES)
def test_an_engine_that_never_finishes_its_reply_is_abandoned_at_the_budget(
    stall_mode: str,
) -> None:
    """Silent, and dripping one byte at a time: the wait ends at the budget."""
    assert STALL_HOLD_SECONDS > SHORT_BUDGET_SECONDS + BUDGET_OVERRUN_TOLERANCE_SECONDS
    engine = StallingEngine(stall_mode, hold_seconds=STALL_HOLD_SECONDS)
    try:
        started = time.monotonic()
        with pytest.raises(TrendReportError) as raised:
            report_red_trend(engine.port, RED_TREND, budget_seconds=SHORT_BUDGET_SECONDS)
        elapsed = time.monotonic() - started
        posts_seen = engine.post_count
    finally:
        engine.close()

    assert elapsed >= SHORT_BUDGET_SECONDS * 0.9, (
        f"if it gave up after {elapsed:.2f} s of a {SHORT_BUDGET_SECONDS} s "
        "budget then it is not waiting the budget out - broken"
    )
    assert elapsed < SHORT_BUDGET_SECONDS + BUDGET_OVERRUN_TOLERANCE_SECONDS, (
        f"if a {stall_mode} engine holds the caller for {elapsed:.2f} s against "
        f"a {SHORT_BUDGET_SECONDS} s budget then the bound is not a bound - broken"
    )
    message = str(raised.value)
    assert f"budget {SHORT_BUDGET_SECONDS:.1f} s" in message, message
    assert "waiting" in message, message
    expected_attempts = f"{TREND_REPORT_ATTEMPTS} attempt"
    assert expected_attempts in message, (
        "if the give-up message does not say how many attempts it made then "
        f"broken: {message}"
    )
    if stall_mode == STALL_HEALTH_SILENT:
        assert HEALTH_PATH in message
        assert posts_seen == 0, "nothing may be posted to an engine that never identified itself"
    else:
        assert CLIENT_ERRORS_PATH in message
        assert posts_seen == TREND_REPORT_ATTEMPTS == 1, (
            f"if {posts_seen} POSTs reached an engine that never answered then "
            "the report was retried into a sink that does not dedupe - broken"
        )
        assert "may still" in message, (
            "if a POST that was sent and went unanswered is reported as "
            f"definitely unrecorded then the message claims more than it knows: {message}"
        )


def test_a_budget_that_is_not_positive_is_refused_before_anything_is_sent() -> None:
    engine = StallingEngine(STALL_HEALTH_SILENT, hold_seconds=STALL_HOLD_SECONDS)
    try:
        with pytest.raises(ValueError, match="budget_seconds"):
            report_red_trend(engine.port, RED_TREND, budget_seconds=0.0)
    finally:
        engine.close()
