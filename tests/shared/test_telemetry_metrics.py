"""OBS-9056: the engine metrics aggregator and its ingest route.

[if] a batch is oversized, carries an unlisted tag or arrives with consent declined [then] it is refused, the tag dropped or nothing forwarded, [else stop].

Each test names the failure it exists to catch ("if X then broken").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.telemetry.consent import (
    TERMS_VERSION,
    configure_consent_gate,
    read_consent,
    reset_consent_for_tests,
)
from apps.shared.telemetry.metrics import (
    MAX_LOCAL_ROWS_PER_DAY,
    MAX_SERIES_PER_WINDOW,
    METRICS,
    OVERFLOW_SERIES,
    MetricsAggregator,
)
from apps.webui.server.routes import telemetry_consent, telemetry_metrics

pytestmark = pytest.mark.requirement("OBS-9056")

BROWSER_BATCH = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "browser-metric-batch.json"


@pytest.fixture(autouse=True)
def _clean_metrics() -> Any:
    """Start and end every test with an empty aggregator and consent granted."""
    METRICS.reset_for_tests()
    configure_consent_gate(required=True, granted=True)
    yield
    METRICS.reset_for_tests()
    reset_consent_for_tests()


def _series(name: str) -> list[dict[str, Any]]:
    """The current window's rows for one metric name."""
    return [row for row in METRICS.snapshot() if row["name"] == name]


def _app(consent_dir: Path | None = None) -> TestClient:
    """A client for an app carrying the production metrics and consent routers."""
    app = FastAPI()
    app.include_router(telemetry_metrics.router, prefix="/api/v1")
    app.include_router(telemetry_consent.router, prefix="/api/v1")
    app.state.telemetry_consent_dir = consent_dir
    return TestClient(app)


# ----- aggregator --------------------------------------------------------------
def test_percentiles_report_bucket_upper_bounds() -> None:
    """if p95 of 95 fast and 5 slow samples reads fast then broken."""
    agg = MetricsAggregator()
    for _ in range(95):
        agg.record("x.op", 3.0)
    for _ in range(5):
        agg.record("x.op", 4000.0)
    (row,) = agg.snapshot()
    assert row["count"] == 100
    assert row["p50_ms"] == 5
    assert row["p95_ms"] == 5
    assert row["max_ms"] == 4000.0
    agg.record("x.op", 4000.0)
    (row,) = agg.snapshot()
    assert row["p95_ms"] == 5_000, "a 6th slow sample must move p95 into the slow bucket"


def test_untimed_samples_count_without_percentiles() -> None:
    """if an untimed sample invents a percentile or loses its error count then broken."""
    agg = MetricsAggregator()
    agg.record("x.event", tags={"kind": "a"})
    agg.record("x.event", tags={"kind": "a"}, error=True)
    (row,) = agg.snapshot()
    assert (row["count"], row["errors"]) == (2, 1)
    assert "p95_ms" not in row


def test_unknown_tag_keys_are_dropped_and_values_scrubbed() -> None:
    """if a track title or a library path survives as a tag then broken."""
    agg = MetricsAggregator()
    agg.record(
        "x.op",
        1.0,
        tags={
            "track_title": "Quiet Lanterns - Zzyxq",
            "route": "/Users/dev/Music/Quiet Lanterns - Zzyxq.mp3",
            "kind": "load",
        },
    )
    (row,) = agg.snapshot()
    assert "track_title" not in row["tags"]
    assert "Zzyxq" not in json.dumps(row)
    assert row["tags"]["kind"] == "load"


def test_series_cardinality_is_capped() -> None:
    """if a name minted per id grows the window without bound then broken."""
    agg = MetricsAggregator()
    for index in range(MAX_SERIES_PER_WINDOW + 50):
        agg.record("x.op", 1.0, tags={"kind": f"id-{index}"})
    rows = agg.snapshot()
    assert len(rows) == MAX_SERIES_PER_WINDOW + 1
    (overflow,) = [row for row in rows if row["name"] == OVERFLOW_SERIES]
    assert overflow["count"] == 50


def test_bad_names_are_rejected_not_recorded() -> None:
    """if free text is accepted as a metric name then broken."""
    agg = MetricsAggregator()
    agg.record("Track: Quiet Lanterns", 1.0)
    assert agg.snapshot() == [] and agg.rejected_names == 1


def test_flush_writes_local_rows_and_resets(tmp_path: Path) -> None:
    """if a flushed window is not on disk, or is flushed twice, then broken."""
    agg = MetricsAggregator()
    agg.log_dir = tmp_path
    agg.record("x.op", 12.0, tags={"kind": "a"})
    rows = agg.flush()
    assert len(rows) == 1
    files = list(tmp_path.glob("telemetry-metrics-*.jsonl"))
    assert len(files) == 1
    on_disk = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert on_disk[0]["name"] == "x.op" and on_disk[0]["count"] == 1
    assert agg.snapshot() == []
    assert agg.flush() == []
    assert len(files[0].read_text().splitlines()) == 1


def _forwarding_aggregator(sent: list[dict[str, Any]]) -> MetricsAggregator:
    """An aggregator whose forwarder collects every row it is handed into ``sent``."""
    agg = MetricsAggregator()

    def forwarder(rows: list[dict[str, Any]]) -> bool:
        """Collect what would leave the machine."""
        sent.extend(rows)
        return True

    agg.forwarder = forwarder
    return agg


def test_rows_the_local_file_could_not_take_are_not_forwarded(tmp_path: Path) -> None:
    """if Sentry gets a summary the local file never recorded, then broken.

    The log dir path is a regular file, so the real mkdir fails.
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x")
    sent: list[dict[str, Any]] = []
    agg = _forwarding_aggregator(sent)
    agg.log_dir = blocked
    agg.record("x.op", 1.0)
    assert len(agg.flush()) == 1
    assert sent == []
    # It is dropped, not held: a later window that does land goes alone.
    agg.log_dir = tmp_path / "logs"
    agg.record("y.op", 1.0)
    agg.flush()
    assert [row["name"] for row in sent] == ["y.op"]


def test_rows_past_the_daily_local_cap_are_not_forwarded(tmp_path: Path) -> None:
    """if rows past the local file's daily cap still reach Sentry, then broken."""
    sent: list[dict[str, Any]] = []
    agg = _forwarding_aggregator(sent)
    agg.log_dir = tmp_path
    agg.record("x.op", 1.0)
    agg.flush()
    assert [row["name"] for row in sent] == ["x.op"], "control: a written row goes on"
    agg._local_rows = MAX_LOCAL_ROWS_PER_DAY - 1
    agg.record("a.op", 1.0)
    agg.record("b.op", 1.0)
    agg.flush()
    [metrics_file] = tmp_path.glob("telemetry-metrics-*.jsonl")
    on_disk = [json.loads(line)["name"] for line in metrics_file.read_text().splitlines()]
    assert on_disk == ["x.op", "a.op"]
    assert [row["name"] for row in sent] == ["x.op", "a.op"]
    # The day is full now: a whole window neither lands nor goes.
    agg.record("c.op", 1.0)
    agg.flush()
    assert [row["name"] for row in sent] == ["x.op", "a.op"]


def test_with_no_log_dir_every_row_is_forwarded() -> None:
    """if turning the local file off also stops forwarding, then broken."""
    sent: list[dict[str, Any]] = []
    agg = _forwarding_aggregator(sent)
    agg.record("x.op", 1.0)
    agg.record("y.op", 1.0)
    agg.flush()
    assert sorted(row["name"] for row in sent) == ["x.op", "y.op"]


def test_forwarder_refusal_keeps_summaries_pending() -> None:
    """if a window held for a live set is dropped instead of sent later then broken."""
    agg = MetricsAggregator()
    sent: list[list[dict[str, Any]]] = []
    allow = {"now": False}

    def forwarder(rows: list[dict[str, Any]]) -> bool:
        """Refuse until ``allow`` flips, then collect each batch."""
        if not allow["now"]:
            return False
        sent.append(rows)
        return True

    agg.forwarder = forwarder
    agg.record("x.op", 1.0)
    agg.flush()
    assert sent == []
    allow["now"] = True
    agg.record("x.op", 2.0)
    agg.flush()
    assert len(sent) == 1 and len(sent[0]) == 2, "the held window rides the next send"


def test_merge_folds_an_external_summary() -> None:
    """if a Rust-side histogram lands with the wrong percentiles or is guessed when malformed then broken."""
    from apps.shared.telemetry.metrics import BUCKETS_MS

    agg = MetricsAggregator()
    buckets = [0] * (len(BUCKETS_MS) + 1)
    buckets[BUCKETS_MS.index(50)] = 19
    buckets[BUCKETS_MS.index(1000)] = 1
    assert agg.merge(
        "engine.command", tags={"command": "load"}, count=20, errors=1, buckets=buckets, total_ms=1500.0, max_ms=900.0
    )
    agg.record("engine.command", 10.0, tags={"command": "load"})
    (row,) = agg.snapshot()
    assert (row["count"], row["errors"]) == (21, 1)
    assert row["p50_ms"] == 50 and row["max_ms"] == 900.0
    assert not agg.merge("engine.command", count=1, buckets=[1, 2])
    assert not agg.merge("engine.command", count=1, errors=2)


# ----- the ingest route ----------------------------------------------------------
def test_the_ingest_route_takes_a_batch_the_browser_sink_really_posted() -> None:
    """if the engine refuses or mis-files the browser's own batch, then broken."""
    batch = json.loads(BROWSER_BATCH.read_text(encoding="utf-8"))
    client = _app()
    response = client.post("/api/v1/telemetry/metrics", json=batch)
    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": len(batch["samples"]), "rejected": 0}
    (http,) = _series("http.client.duration")
    assert http["tags"] == {**batch["samples"][0]["tags"], "origin": "browser"}
    (deck,) = _series("deck.load")
    assert deck["tags"] == {"origin": "browser"} and deck["count"] == 1
    # Control: the route does refuse what the browser must never send.
    bad = {**batch, "origin": "somewhere-else"}
    assert client.post("/api/v1/telemetry/metrics", json=bad).status_code == 422


def test_a_batch_over_the_sample_cap_is_rejected_and_records_nothing() -> None:
    """if a batch over 500 samples is accepted, or partly recorded, then broken."""
    sample = {"name": "x.op", "value_ms": 1.0, "tags": {}, "error": False}
    cap = telemetry_metrics.MAX_SAMPLES_PER_BATCH
    assert cap == 500
    client = _app()
    over = {"origin": "browser", "samples": [sample] * (cap + 1)}
    response = client.post("/api/v1/telemetry/metrics", json=over)
    assert 400 <= response.status_code < 500, response.text
    assert METRICS.snapshot() == []
    # Control: exactly the cap is taken.
    at_cap = {"origin": "browser", "samples": [sample] * cap}
    assert client.post("/api/v1/telemetry/metrics", json=at_cap).json() == {"accepted": cap, "rejected": 0}


def test_a_tag_outside_the_allowlist_is_dropped_before_forwarding() -> None:
    """if a posted tag key outside the allowlist reaches the forwarder, then broken."""
    sent: list[dict[str, Any]] = []

    def forwarder(rows: list[dict[str, Any]]) -> bool:
        """Collect what would leave the machine."""
        sent.extend(rows)
        return True

    METRICS.forwarder = forwarder
    tags = {"channel": "ipc", "kind": "load", "outcome": "ok", "program": "odj-audio", "track_title": "Zzyxq"}
    batch = {"origin": "shell", "samples": [{"name": "x.op", "value_ms": 3.0, "tags": tags}]}
    assert _app().post("/api/v1/telemetry/metrics", json=batch).status_code == 202
    METRICS.flush()
    (row,) = sent
    assert row["tags"] == {"channel": "ipc", "kind": "load", "outcome": "ok", "program": "odj-audio", "origin": "shell"}
    assert "Zzyxq" not in json.dumps(sent)


def test_with_consent_declined_in_the_consent_file_nothing_is_forwarded(tmp_path: Path) -> None:
    """if a summary leaves while the consent file says declined, then broken.

    The decision goes through the production consent route, which writes the
    real consent file and updates the gate; the gate is then rebuilt from that
    file the way boot does, and the metrics go through the production route.
    """
    sent: list[dict[str, Any]] = []

    def forwarder(rows: list[dict[str, Any]]) -> bool:
        """Collect what would leave the machine."""
        sent.extend(rows)
        return True

    METRICS.forwarder = forwarder
    METRICS.log_dir = tmp_path / "logs"
    client = _app(tmp_path)
    put = client.put("/api/v1/telemetry/consent", json={"decision": "declined", "terms_version": TERMS_VERSION})
    assert put.status_code == 200, put.text
    assert json.loads((tmp_path / "telemetry-consent.json").read_text())["decision"] == "declined"
    configure_consent_gate(required=True, granted=read_consent(tmp_path).decision == "accepted")
    batch = json.loads(BROWSER_BATCH.read_text(encoding="utf-8"))
    assert client.post("/api/v1/telemetry/metrics", json=batch).status_code == 202
    assert len(METRICS.flush()) == 2
    assert sent == []
    [local] = (tmp_path / "logs").glob("telemetry-metrics-*.jsonl")
    assert len(local.read_text().splitlines()) == 2, "declined still keeps the local record"
    # Control: accepting through the same route releases the held window.
    put = client.put("/api/v1/telemetry/consent", json={"decision": "accepted", "terms_version": TERMS_VERSION})
    assert put.status_code == 200, put.text
    METRICS.flush()
    assert sorted(row["name"] for row in sent) == ["deck.load", "http.client.duration"]


def test_the_production_app_mounts_the_metrics_route() -> None:
    """if the engine app does not serve /api/v1/telemetry/metrics, then broken."""
    from apps.webui.server import routes as routes_package
    from apps.webui.server.app_wiring import _routers_under

    assert telemetry_metrics.router in _routers_under(routes_package)
