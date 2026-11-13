"""PERFREADOUT-12579: GET /performance/telemetry/readouts serves the card rows the page posted.

- [if] no sample was posted, or the last one carried no rows [then] the route answers 404, [else stop].
- [if] a sample carried readout_rows [then] the route returns those rows unchanged, [else stop].
- [if] a sample carries more than four rows or an unknown key [then] the sink refuses it, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("PERFREADOUT-12579")

SAMPLES = "/api/v1/performance/telemetry/client-samples"
READOUTS = "/api/v1/performance/telemetry/readouts"


def readout_rows() -> list[dict[str, str]]:
    """[if] a test needs card rows [then] these are four in the perfReadoutRows() shape, [else stop]."""
    return [
        {"key": key, "name": f"{key} name", "value": value, "explainer": f"{key} explainer", "detail": "d"}
        for key, value in (("hz", "--"), ("cache", "0"), ("waveform", "W--"), ("memory", "63M"))
    ]


def client_sample(rows: list[dict[str, str]] | None) -> dict[str, Any]:
    """[if] a test needs a sample [then] this is a minimal valid one carrying ``rows``, [else stop]."""
    return {
        "client_sample_id": "s-1",
        "client_session_id": "session-1",
        "client_timestamp": "2026-11-13T10:00:00.000Z",
        "route": "/",
        "page_uptime_ms": 1000,
        "js_heap_mb": None,
        "pcm_estimated_mb": 0,
        "anlz_estimated_mb": 0,
        "anlz_entry_count": 0,
        "prefetch_mb": 0,
        "prefetch_count": 0,
        "audio_health_hz": None,
        "audio_health_level": "idle",
        "perf_event_count": 0,
        "decks": [
            {
                "deck_id": deck_id,
                "playing": False,
                "audible": False,
                "transport_pending": False,
                "stem_status": "unavailable",
            }
            for deck_id in (1, 2, 3, 4)
        ],
        "readout_rows": rows,
    }


@pytest.fixture
def client(tmp_path: Any) -> TestClient:
    """[if] a test needs the app [then] this is a fresh one logging to tmp_path, [else stop]."""
    app = create_app(backend=InMemoryBackend(), mount_frontend=False, enable_cors=False)
    app.state.performance_log_dir = tmp_path
    return TestClient(app)


def test_readouts_404_before_any_sample(client: TestClient) -> None:
    """[if] telemetry has not been posted [then] the route answers 404, not empty rows, [else stop]."""
    response = client.get(READOUTS)
    assert response.status_code == 404
    assert "no performance readout rows" in response.json()["detail"]


def test_readouts_404_when_the_last_sample_had_no_rows(client: TestClient) -> None:
    """[if] the last sample carried no rows [then] the route answers 404, [else stop]."""
    assert client.post(SAMPLES, json=client_sample(None)).status_code == 202
    assert client.get(READOUTS).status_code == 404


def test_readouts_return_the_posted_rows(client: TestClient) -> None:
    """[if] a sample carried rows [then] the route returns them unchanged with its event id, [else stop]."""
    posted = client.post(SAMPLES, json=client_sample(readout_rows()))
    response = client.get(READOUTS)
    assert response.status_code == 200
    assert response.json() == {"event_id": posted.json()["event_id"], "rows": readout_rows()}


def test_sink_refuses_too_many_rows_or_unknown_keys(client: TestClient) -> None:
    """[if] the rows exceed four or invent a key [then] the sink refuses them with 422, [else stop]."""
    assert client.post(SAMPLES, json=client_sample(readout_rows() + readout_rows()[:1])).status_code == 422
    bad = readout_rows()
    bad[0]["key"] = "fps"
    assert client.post(SAMPLES, json=client_sample(bad)).status_code == 422
